import os
from os.path import join
from datetime import datetime
import multiprocessing
from pathlib import Path
import shutil
from numbers import Number
from typing import Union, Sequence, List
from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
import matplotlib.pyplot
import numpy as np
from omegaconf import DictConfig, open_dict
import rasterio
from shapely.geometry import box
from solaris import vector
from tqdm import tqdm
from osgeo import gdal, ogr
from torch.utils.data import DataLoader
from torchgeo.samplers import GridGeoSampler
from torchgeo.datasets import stack_samples

from dataset.create_dataset import DRDataset, GDLVectorDataset
from dataset.aoi import AOI
from utils.aoiutils import aois_from_csv
from utils.geoutils import check_gdf_load, check_rasterio_im_load, bounds_gdf, bounds_riodataset, mask_nodata, \
    nodata_vec_mask
from utils.utils import get_key_def, get_git_hash
from utils.verifications import validate_raster
# Set the logging file
from utils import utils
logging = utils.get_logger(__name__)  # import logging
# Set random seed for reproducibility
np.random.seed(123)


def annot_percent(img_patch: Union[str, Path, rasterio.DatasetReader],
                  gdf_patch: Union[str, Path, gpd.GeoDataFrame],
                  ):
    """
    Calculate percentage of values in GeoDataFrame that contain classes other than background
    @param img_patch: str, Path or rasterio.DatasetReader
    @param gdf_patch: str, Path or gpd.GeoDataFrame
    @return: (int) Annotated percent
    """
    gdf_patch = check_gdf_load(gdf_patch)
    if gdf_patch.empty:
        return 0
    img_patch_dataset = check_rasterio_im_load(img_patch)

    gdf_patch_bounds, img_patch_bounds = bounds_gdf(gdf_patch), bounds_riodataset(img_patch_dataset)

    if not gdf_patch_bounds.intersects(img_patch_bounds):
        raise rasterio.errors.CRSError(
            f"Features in label file {gdf_patch.info} do not intersect with bounds of raster file "
            f"{img_patch_dataset.files}")

    patch_bounds = box(*img_patch_dataset.bounds)

    gdf_patch_cea = gdf_patch.geometry.to_crs({'proj': 'cea'})  # Cylindrical equal-area projection
    annot_ct_vec = gdf_patch_cea.area.sum()
    annot_perc = annot_ct_vec / patch_bounds.area
    return annot_perc * 100


class Tiler(object):
    def __init__(self,
                 tiling_root_dir: Union[Path, str],
                 src_aoi_list: List = None,
                 patch_size: int = 1024,
                 patch_stride: int = None,
                 min_annot_perc: Number = 0,
                 val_percent: int = None,
                 debug: bool = False,
                 write_mode: str = "raise_exists"):
        """
        @param tiling_root_dir: pathlib.Path or str
            Root directory under which all patches will be written (in subfolders)
        @param src_aoi_list: list
            List of source data to be patched. Must be instances of AOI class.
            AOI objects contain properties including paths of source data and other data-related info.
        @param patch_size: int, optional
            Size of patches to output. Defaults to 1024
        @param patch_stride: int, optional
            Number of pixels between each patch. Defaults to patch_size
            without remainder. Rasterio will use bilinear resampling. Defaults to 1 (no resampling).
        @param min_annot_perc: Number, optional
            If ground truth patch above this minimum annotated percentage,
            the gt patch will be kept in final dataset
        @param val_percent: integer, optional
            Percentage of training patches that should be written to validation set
        @param debug: boolean, optional
            If True, activate debug functionality
        @param write_mode: str, optional
            If "raise_exists" (default), tiling will raise error if patches already exist.
            if "append", tiling will skip AOIs for which all patches already exist.
        """
        if src_aoi_list and not isinstance(src_aoi_list, List):
            raise TypeError('Input data should be a List')
        self.src_aoi_list = src_aoi_list
        if not isinstance(tiling_root_dir, (Path, str)):
            raise TypeError(f'Tiles root directory should be a of class pathlib.Path or a string.\n'
                            f'Got {tiling_root_dir} of type {type(tiling_root_dir)}')
        if not Path(tiling_root_dir).parent.is_dir():
            raise FileNotFoundError(f'{Path(tiling_root_dir).parent} is not a valid directory')
        self.tiling_root_dir = Path(tiling_root_dir)
        self.tiling_root_dir.mkdir(exist_ok=True)

        self.for_inference = False
        splits_set = set([aoi.split for aoi in src_aoi_list])
        if 'inference' in splits_set:
            logging.warning(f'At least one AOI was tagged for inference (only imagery, without ground truth). \n'
                            f'Tiler will consider only imagery from now on. \n'
                            f'Set of splits present in AOIs: {splits_set}')
            self.for_inference = True

        self.datasets = list(splits_set)
        if 'trn' in self.datasets:
            self.datasets.append('val')

        if not isinstance(patch_size, int):
            raise TypeError(f'Tile size should be an integer. Got {patch_size} of type {type(patch_size)}')
        self.dest_patch_size = patch_size

        # Tile stride defaults to patch size
        if not patch_stride:
            patch_stride = self.dest_patch_size
        if not isinstance(patch_stride, int):
            raise TypeError(f'Tile stride should be an integer. Got {patch_stride} of type {type(patch_stride)}')
        self.patch_stride = patch_stride

        if not isinstance(min_annot_perc, Number) and 0 <= min_annot_perc <= 100:
            raise TypeError(f'Minimum annotated percent should be a number between 0 and 100.\n'
                            f'Got {min_annot_perc} of type {type(min_annot_perc)}')
        self.min_annot_perc = min_annot_perc

        try:
            bands_set = set([tuple(aoi.raster_bands_request) for aoi in self.src_aoi_list])
            self.bands_requested = self.src_aoi_list[0].raster_bands_request
        except TypeError:
            bands_set = set([aoi.raster_meta['count'] for aoi in self.src_aoi_list])
            self.bands_requested = list(range(1, self.src_aoi_list[0].raster_meta['count']+1))
        if len(bands_set) > 1:
            raise ValueError(f'Bands requested vary among submitted AOIs. \n'
                             f'Check source imagery and define a unique list of bands to keep. \n'
                             f'Set of bands requested: {bands_set}')
        self.bands_num = len(self.bands_requested)


        if val_percent and not isinstance(val_percent, int):
            raise TypeError(f'Validation percentage should be an integer.\n'
                            f'Got {val_percent} of type {type(val_percent)}')
        self.val_percent = val_percent

        if all(aoi.attr_values_filter is not None for aoi in self.src_aoi_list):
            attr_vals_set = set([tuple(aoi.attr_values_filter) for aoi in self.src_aoi_list])
            if len(attr_vals_set) > 1:
                raise ValueError(f'Multiple attribute values used to filter ground truth features were found. \n'
                                 f'Set of attribute values requested: {attr_vals_set}')
        self.attr_vals_exp = self.src_aoi_list[0].attr_values_filter

        if not isinstance(write_mode, str) and write_mode not in ["raise_exists", "append"]:
            raise ValueError(f"Tiler's write mode should be \"raise_exists\" or \"append\". See docs.")
        self.write_mode = write_mode
        self.debug = debug

        self.datasets_dir = []
        mod_time = datetime.fromtimestamp(self.tiling_root_dir.stat().st_mtime).strftime('%Y%m%d-%H%M%S')
        if len(list(self.tiling_root_dir.iterdir())) > 0:
            if debug:
                move_dir = self.tiling_root_dir / mod_time
                move_dir.mkdir(exist_ok=True)
                # Move existing data folder with a timestamp suffix.
                for item in self.tiling_root_dir.glob('*.csv'):
                    shutil.move(item, move_dir)
                for dataset in self.datasets:
                    if (self.tiling_root_dir / dataset).is_dir():
                        shutil.move(self.tiling_root_dir / dataset, move_dir)
            elif self.write_mode == "raise_exists":
                raise FileExistsError(
                    f'Patches directory is not empty. Won\'t overwrite existing content unless debug=True.\n'
                    f'Directory: {self.tiling_root_dir}.'
                )
            elif self.write_mode == "append":
                logging.info(f"Append mode: Will skip AOIs where tiling is complete.")

        for dataset in self.datasets:
            dataset_dir = self.tiling_root_dir / dataset
            dataset_dir.mkdir(exist_ok=True)
            self.datasets_dir.append(dataset_dir)

        logging.info(f'Patches will be written to {self.tiling_root_dir}\n\n')

    @staticmethod
    def make_dataset_file_name(exp_name: str, min_annot: Number, dataset: str, attr_vals: Sequence = None):
        if isinstance(attr_vals, int):
            attr_vals = [attr_vals]
        vals = "_feat" + "-".join([str(val) for val in attr_vals]) if attr_vals else ""
        min_annot_str = f"_min-annot{min_annot}"
        sampling_str = vals + min_annot_str
        dataset_file_name = f'{exp_name}{sampling_str}_{dataset}.csv'
        return dataset_file_name, sampling_str

    @staticmethod
    def _save_tile(sample, dst, window, crs):
        """
        Save individual raster patch as a geotiff.
        Args:
            sample: numpy array with a shape of (h, w, c).
            dst: destination file path.
            window: bounding box coordinates of the patch.
            crs: patch's crs.

        Returns: None
        """
        # Unpack the bounding box coordinates and the sample's dimensions.
        xmin, ymax, xmax, ymin = window
        n_rows, n_cols, n_bands = sample.shape[1], sample.shape[2], sample.shape[0]

        # Calculate spatial resolution based on the extracted data:
        xres = (xmax - xmin) / float(n_cols)
        yres = (ymax - ymin) / float(n_rows)
        # Set the geotransformation parameters in the GDAL format:
        geotransform = (xmin, xres, 0, ymax, 0, -yres)

        # Create an output raster patch datasource:
        drv = gdal.GetDriverByName("GTiff")
        gdal_type = gdal.GDT_Byte if sample.dtype == np.uint8 else gdal.GDT_UInt16
        dst_ds = drv.Create(dst, n_cols, n_rows, n_bands, gdal_type, options=["COMPRESS=DEFLATE"])

        # Write every channel of the raster patch array to the corresponding band of the output datasource:
        for band in range(n_bands):
            dst_ds.GetRasterBand(band + 1).WriteArray(sample[band, :, :])

        # Set the datasource projection and geotransformations:
        dst_ds.SetGeoTransform(geotransform)
        dst_ds.SetProjection(crs)

        # Close and save the datasource:
        dst_ds = None

    def _parse_torchgeo_batch(self, batch: dict, nodataval: int):
        """
        Extract data from the TorchGeo batch.
        Args:
            batch: TorchGeo batch.

        Returns: image raster sample as a numpy array, sample CRS, sample bounding box coordinates.
        If the mode is not "inference", then also returns a vector mask.

        """
        # Get the image as an array:
        sample_image = batch['image']
        if not self.for_inference:
            sample_mask = batch['mask'][0]
        else:
            sample_mask = None

        sample_image = np.asarray(sample_image).squeeze(0)
        # Calculate % of nodata pixels:
        num_nodata = np.count_nonzero(sample_image == nodataval)
        if num_nodata / sample_image.size == 1:
            return None

        # Get the CRS and the bounding box coordinates:
        sample_crs = batch['crs'][0].wkt
        window = ([batch['bbox'][0][0], batch['bbox'][0][3], batch['bbox'][0][1], batch['bbox'][0][2]])

        return sample_image, sample_mask, sample_crs, window

    @staticmethod
    def _define_output_name(aoi, output_folder, window):
        """
        Generate the output file name without the file extention.
        Args:
            aoi: current AOI object.
            output_folder: output folder name.
            window: current bounding box coordinates.

        Returns: output file name without the file extention

        """
        out_name = aoi.raster_name.stem + "_" + "_".join([str(x).replace(".", "_") for x in window[:2]])
        return join(output_folder, out_name)

    @staticmethod
    def _save_vec_mem_tile(mem_ds, output_vector_name):
        # Crate the output vector patch datasource:
        driver_name = "GeoJSON"
        driver = ogr.GetDriverByName(driver_name)
        out_ds = driver.CreateDataSource(output_vector_name)
        mem = out_ds.CopyLayer(mem_ds.GetLayer(), '0')

        # Save and close datasources:
        out_ds = None
        mem_ds = None

    def tiling_per_aoi(
            self,
            aoi: AOI,
            out_img_dir: Union[str, Path],
            out_label_dir: Union[str, Path] = None,
    ):
        """
        Generates grid patches from the AOI.raster using TorchGeo's GeoGridSampler dataloader.
        Generates grid patches from the AOI.label using GDAL/OGR.

        @param aoi: AOI object to be tiled
        @param out_img_dir: path to output patched images directory
        @param out_label_dir: optional, path to output patched labels directory
        @return: written patches to output directories as .tif for imagery and .geojson for label.

        https://torchgeo.readthedocs.io/en/stable/tutorials/custom_raster_dataset.html
        https://torchgeo.readthedocs.io/en/stable/api/samplers.html
        https://gdal.org/api/python/osgeo.gdal.html
        https://gdal.org/api/python/osgeo.ogr.html
        """
        if not aoi.raster:  # in case of multiprocessing
            aoi.raster = rasterio.open(aoi.raster_dest)
        nodata = aoi.raster.nodata

        # Create TorchGeo-based custom DRDataset dataset:
        dr_dataset = DRDataset(aoi.raster)

        if not self.for_inference:
            nodata_mask = nodata_vec_mask(raster=aoi.raster, nodata_val=nodata)
            vec_dataset = GDLVectorDataset(vec_ds=aoi.label, nodata_mask=nodata_mask)
            # Combine raster and vector datasets using AND operator (sampling only from the intersection area).
            resulting_dataset = dr_dataset & vec_dataset
            os.makedirs(out_label_dir, exist_ok=True)
        else:
            resulting_dataset = dr_dataset

        # Initialize a sampler and a dataloader. If we need overlapping, stride must be adjusted accordingly.
        # For now, having stride parameter equal to the size, we have no overlapping (except for the borders).
        sampler = GridGeoSampler(resulting_dataset, size=self.dest_patch_size, stride=self.dest_patch_size)
        dataloader = DataLoader(resulting_dataset, sampler=sampler, collate_fn=stack_samples)

        if len(dataloader) == 0:
            raise ValueError("The dataloader is empty. Check input image and vector datasets.")

        raster_tile_paths = []
        vector_tile_paths = []

        skip_aoi = False
        if self.write_mode == "append" and out_img_dir.is_dir():
            expect_patch_len = len(dataloader)
            if aoi.split == "trn":  # Need to move val patches back to trn. Trn/val sorting will restart from scratch.
                self.move_existing_val_to_trn(out_img_dir_trn=out_img_dir, out_lbl_dir_trn=out_label_dir)
            raster_tile_paths = list(out_img_dir.iterdir())
            vector_tile_paths = list(out_label_dir.iterdir()) if out_label_dir is not None else []
            logging.info(f"[no overwrite mode]\nPatches found for AOI {aoi.aoi_id}:\n"
                         f"Imagery: {len(raster_tile_paths)} / {expect_patch_len}\n"
                         f"Ground truth: {len(vector_tile_paths)} / {expect_patch_len}")
            if len(raster_tile_paths) == expect_patch_len and \
                    (len(vector_tile_paths) == expect_patch_len or not vector_tile_paths):
                logging.info(f"Skipping tiling for {aoi.aoi_id}")
                skip_aoi = True
            else:
                [os.remove(ras_patch) for ras_patch in raster_tile_paths]
                [os.remove(vec_patch) for vec_patch in vector_tile_paths]
                raster_tile_paths = []
                vector_tile_paths = []

        if not skip_aoi:
            # Iterate over the dataloader and save resulting raster patches:
            bboxes = []
            os.makedirs(out_img_dir, exist_ok=True)

            raster_tile_data = []
            for i, batch in tqdm(enumerate(dataloader), total=len(dataloader)):
                # Parse the TorchGeo batch:
                tile_data = self._parse_torchgeo_batch(batch, nodata)

                if tile_data is None:
                    continue

                sample_image, sample_mask, sample_crs, sample_window = tile_data
                bboxes.append(sample_window)

                # Define the output raster patch filename:
                dst_raster_name = self._define_output_name(aoi, out_img_dir, sample_window) + ".tif"
                raster_tile_paths.append(dst_raster_name)

                # Append all the raster patch data for later parallel writing to the disk:
                raster_tile_data.append([sample_image, dst_raster_name, sample_window, sample_crs])
                if not self.for_inference:
                    # Define the output vector patch filename:
                    dst_vector_name = self._define_output_name(aoi, out_label_dir, sample_window) + ".geojson"
                    vector_tile_paths.append(dst_vector_name)
                    # Clip vector labels having bounding boxes from the raster patches:
                    self._save_vec_mem_tile(sample_mask, dst_vector_name)

            # Write all raster tiles to the disk in parallel:
            logging.info(f'Cropping raster patches...')
            with ThreadPoolExecutor(32) as exe:
                _ = [exe.submit(self._save_tile, *args) for args in raster_tile_data]

        aoi.close_raster()  # for multiprocessing
        aoi.raster = None

        return aoi, sorted(raster_tile_paths), sorted(vector_tile_paths)

    def passes_min_annot(self,
                         img_patch: Union[str, Path],
                         gt_patch: Union[str, Path, gpd.GeoDataFrame]
                         ):
        """
        Decides whether a patch pair should be kept based on minimum annotated percent threshold (i.e. maximum background
        proportion). This filter applies to trn and val datasets only, i.e. all patches from tst dataset are included
        """
        map_img_gdf = check_gdf_load(gt_patch)
        annot_perc = annot_percent(
            img_patch=img_patch,
            gdf_patch=map_img_gdf,
        )
        if annot_perc >= self.min_annot_perc:
            return True, annot_perc
        else:
            logging.debug(f"Ground truth patch in trn/val dataset doesn't reach minimum annotated percentage.\n"
                          f"Ground truth patch: {gt_patch}\n"
                          f"Annotated percentage: {annot_perc}\n"
                          f"Minimum annotated percentage: {self.min_annot_perc}")
            return False, annot_perc

    def get_burn_gt_patch_path(self, attr_vals: Sequence, gt_patch: Union[str, Path]):
        _, patches_str = self.make_dataset_file_name(None, self.min_annot_perc, None, attr_vals)
        out_burned_gt_path = Path(gt_patch).parent.parent / 'labels_burned' / f'{Path(gt_patch).stem}{patches_str}.tif'
        out_burned_gt_path.parent.mkdir(exist_ok=True)
        return out_burned_gt_path

    def burn_gt_patch(self, aoi: AOI,
                      img_patch: Union[str, Path],
                      gt_patch: Union[gpd.GeoDataFrame, str, Path],
                      out_px_mask: Union[str, Path],
                      continuous: bool = True,
                      save_preview: bool = True,
                      ):
        """
        Burns a ground truth patch to raster
        @param aoi: AOI object
        @param img_patch: str or pathlib.Path
            Path to image patch
        @param gt_patch: str, pathlib.Path or gpd.GeoDataFrame
            Path to ground truth patch or gpd.GeoDataFrame of ground truth
        @param out_px_mask: Burned patch output path
        @param continuous: bool, if True, burn values will be continuous starting at 1 for easier use in training an ML
                           model (0 being reserved for background class)
        @param save_preview: bool, if True, a copy of the burned label will be created for quick preview from a file
                             manager. Burn values will be stretched to 255.
        @return:
        """
        out_px_mask = Path(out_px_mask)
        gt_patch_gdf = check_gdf_load(gt_patch)
        if out_px_mask.is_file():
            logging.info(f'Burned ground truth patch exists: {out_px_mask}')
            return
        if not aoi.attr_field_filter and aoi.attr_values_filter is not None:
            raise ValueError(f'Values for an attribute field have been provided, but no attribute field is set.\n'
                             f'Attribute values: {aoi.attr_values_filter}')
        elif aoi.attr_field_filter is not None and not aoi.attr_values_filter:
            raise ValueError(f'An attribute field has been provided, but no attribute values were set.\n'
                             f'Attribute field: {aoi.attr_field_filter}. If all values from attribute fields are '
                             f'to be kept, please input full list of values in dataset configuration.')
        # Burn value in attribute field from which features are being filtered
        burn_field = aoi.attr_field_filter if aoi.attr_field_filter else None
        # no attribute field or val given means all values should be burned to 1
        burn_val = 1 if not aoi.attr_field_filter and not aoi.attr_values_filter else None
        if gt_patch_gdf.empty:
            burn_field = None
        elif aoi.attr_field_filter:
            # Define new column 'burn_val' with continuous values for use during burning
            cont_vals_dict = {src: (dst+1 if continuous else src) for dst, src in enumerate(aoi.attr_values_filter)}
            if all(isinstance(val, str) for val in gt_patch_gdf[aoi.attr_field_filter].unique().tolist()):
                cont_vals_dict = {str(src): dst for src, dst in cont_vals_dict.items()}
            gt_patch_gdf['burn_val'] = gt_patch_gdf[aoi.attr_field_filter].map(cont_vals_dict)
            burn_field = 'burn_val'  # overwrite burn_field
        # burn to raster
        vector.mask.footprint_mask(df=gt_patch_gdf, out_file=str(out_px_mask),
                                   reference_im=str(img_patch),
                                   burn_field=burn_field,
                                   burn_value=burn_val)
        if save_preview:
            # burn preview to raster in dedicated folder
            prev_out_px_mask = Path(f'{out_px_mask.parent}_preview') / f'{out_px_mask.stem}.png'
            prev_out_px_mask.parent.mkdir(exist_ok=True)
            with rasterio.open(out_px_mask) as burned_patch:
                burned_patch_array = burned_patch.read()[0, ...]
                matplotlib.pyplot.imsave(prev_out_px_mask, burned_patch_array)

    def filter_and_burn_dataset(
            self,
            aoi: AOI,
            img_patch: Union[str, Path],
            gt_patch: Union[str, Path],
            continuous_vals: bool = True,
            save_preview_labels: bool = True,
    ):
        """
        Randomly sorts between trn and val splits (based on requested desired val's dataset proportion,
        filters a patch pair based on threshold condition (ex.: minimum annotated percentage),
        and burns ground truth patch to raster.
        @param aoi: AOI
            AOI object referencing source data
        @param img_patch: str or Path
            Image patch, mainly as reference for it's bounds
        @param gt_patch: str or Path
            Ground truth vector patch
        @param continuous_vals: bool
            if True, burned pixels values on ground truth will be continuous if they were not to in vector patch
        @param save_preview_labels:
            if True, a colorized "preview" copy of burned labels will be saved as png for quick visualization
        @return:
        """
        if not aoi.raster:  # in case of multiprocessing
            aoi.raster = rasterio.open(aoi.raster_dest)
        nodata = aoi.raster.nodata

        random_val = np.random.randint(1, 101)
        if not {'trn', 'val'}.issubset(set(self.datasets)):
            raise ValueError(f"Tiler should contain a 'trn' and 'val' dataset. Got {self.datasets}")
        # for trn patches, sort between trn and val based on random number
        dataset = 'val' if aoi.split == 'trn' and random_val <= self.val_percent else aoi.split
        if dataset == 'val':  # val dataset
            img_patch = move_patch_trn_to_val(patch=img_patch, src_split=aoi.split, dest_split='val')
            gt_patch = move_patch_trn_to_val(patch=gt_patch, src_split=aoi.split, dest_split='val')
        out_gt_burned_path = self.get_burn_gt_patch_path(attr_vals=aoi.attr_values_filter, gt_patch=gt_patch)
        gdf_patch = AOI.filter_gdf_by_attribute(
            gdf_patch=str(gt_patch),
            attr_field=aoi.attr_field_filter,
            attr_vals=aoi.attr_values_filter
        )
        # measure annotated percentage for all patches as it is useful data analysis info for an output report
        min_annot_success, annot_perc = self.passes_min_annot(
            img_patch=img_patch,
            gt_patch=gdf_patch,
        )
        logging.debug(annot_perc)
        if min_annot_success or dataset == 'tst':
            self.burn_gt_patch(aoi,
                               img_patch=img_patch,
                               gt_patch=gdf_patch,
                               out_px_mask=out_gt_burned_path,
                               continuous=continuous_vals,
                               save_preview=save_preview_labels,
                               )
            # Here nodata pixels will be used to mask corresponding label with "ignore_label"value:
            if isinstance(nodata, int | float):
                mask_nodata(
                    img_patch=img_patch,
                    gt_patch=out_gt_burned_path,
                    nodata_val=int(nodata),
                    mask_val=255
                )

            dataset_line = f'{Path(img_patch).absolute()};{Path(out_gt_burned_path).absolute()};{round(annot_perc)}\n'
            return dataset, dataset_line
        else:
            return dataset, None

    @staticmethod
    def move_existing_val_to_trn(out_img_dir_trn: Union[str, Path], out_lbl_dir_trn: Union[str, Path] = None) -> None:
        """Moves all existing patches from val folder to trn folder"""
        if not Path(out_img_dir_trn).is_dir():
            raise NotADirectoryError(f"Trn directory for imagery doesn't exist.\nGot: {out_img_dir_trn}")
        if not Path(out_lbl_dir_trn).is_dir():
            raise NotADirectoryError(f"Trn directory for ground truth doesn't exist.\nGot: {out_lbl_dir_trn}")
        out_img_dir_val = Path(str(out_img_dir_trn).replace("/trn/", "/val/").replace("\\trn\\", "\\val\\"))
        if out_img_dir_val.is_dir():
            for patch in out_img_dir_val.iterdir():
                patch_dest = Path(str(patch).replace("/val/", "/trn/").replace("\\val\\", "\\trn\\"))
                shutil.move(patch, patch_dest)
        if out_lbl_dir_trn:
            out_label_dir_val = Path(str(out_lbl_dir_trn).replace("/trn/", "/val/").replace("\\trn\\", "\\val\\"))
            if out_label_dir_val.is_dir():
                for patch in out_label_dir_val.iterdir():
                    patch_dest = Path(str(patch).replace("/val/", "/trn/").replace("\\val\\", "\\trn\\"))
                    shutil.move(patch, patch_dest)


def move_patch_trn_to_val(patch: str, src_split: str = "trn", dest_split: str = "val"):
    """Renames and moves a patch's path from on split to another (ex.: to from 'trn' to 'val')"""
    patch_dest = Path(str(patch).replace(src_split, dest_split))
    Path.mkdir(patch_dest.parent, exist_ok=True, parents=True)
    shutil.move(patch, patch_dest)
    return patch_dest


def map_wrapper(x):
    """For multi-threading"""
    return x[0](*(x[1:]))


def main(cfg: DictConfig) -> None:
    """
    Creates training, validation and testing datasets preparation. The tiling process consists of cutting up the imagery
    and ground truth to patches of a certain size. This prepares the dataset for training.

    Patch size

    Size of an individual patch. For example, a raster of 1024 x 1024 pixels will output 4 patches if patch_size is 512.
    The value for this parameter should remain relatively stable as varying patch sizes has little impact of the
    performance of model. Tiling is mostly aimed at making is possible to fill a batch with at least 4 patch pairs
    without busting a machine's memory while training. Defaults to 512.

    Minimum annotated percent

    Discards patch pairs (imagery & ground truth) if the non-background area (e.g. area covered with classes of interest)
    on a given ground truth patch is lower than this minimum. Defaults to 0 (keep all patches). This parameter is a data
    balancing tool for undersampling. It is easy to implement and use, but may not be the perfect solution for all data
    balancing problems. For more information on pros and cons of undersampling, oversampling and other class
    balancing strategies, see [*Buda & Al., 2018*](https://www.sciencedirect.com/science/article/pii/S0893608018302107?casa_token=1gtjUgWc6pUAAAAA:SUDHxtgD8SPDrsM4wR93mH6ZYW57Mr-BYX2nBwxTuT8DsUlWJcvpAV1vgdACQgY78IbiZuCrPgb_)
    and [*Longadge & Dongre, 2013*](https://arxiv.org/pdf/1305.1707).

    Process
    -------
    1. Read csv file and validate existence of all input files and GeoPackages.

    2. Do the following verifications:
        1. Assert number of bands found in raster is equal to desired number
           of bands.
        2. Check that `num_classes` is equal to number of classes detected in
           the specified attribute for each GeoPackage.
           Warning: this validation will not succeed if a Geopackage
                    contains only a subset of `num_classes` (e.g. 3 of 4).
        3. Assert Coordinate reference system between raster and gpkg match.

    3. For each line in the csv file, output patches from imagery and label files based on "patch_size" parameter
    N.B. This step can be parallelized with multiprocessing. Tiling will be skipped if patches already exist.

    4. Create pixels masks from each geojson patch and write a list of image patch / pixelized label patch to text file
    N.B. for train/val datasets, only patches that pass the "min_annot_percent" threshold are kept.

    -------
    :param params: (dict) Parameters found in the yaml config file.
    """
    # PARAMETERS
    bands_requested = get_key_def('bands', cfg['dataset'], default=None, expected_type=Sequence)
    experiment_name = get_key_def('project_name', cfg['general'], default='gdl-training')
    debug = cfg.debug

    # RAW DATA PARAMETERS
    data_dir = get_key_def('raw_data_dir', cfg['dataset'], to_path=True, validate_path_exists=True)
    csv_file = get_key_def('raw_data_csv', cfg['dataset'], to_path=True, validate_path_exists=True)
    download_data = get_key_def('download_data', cfg['dataset'], default=False, expected_type=bool)
    tiling_root_dir = get_key_def('tiling_data_dir', cfg['tiling'], default=data_dir, to_path=True)

    # TILING PARAMETERS
    patch_size = get_key_def('patch_size', cfg['tiling'], default=512, expected_type=int)
    min_annot_perc = get_key_def('min_annot_perc', cfg['tiling'], expected_type=Number, default=0)
    continuous_vals = get_key_def('continuous_values', cfg['tiling'], default=True)
    save_prev_labels = get_key_def('save_preview_labels', cfg['tiling'], default=True)
    write_mode = get_key_def('write_mode', cfg['tiling'], default="raise_exists", expected_type=str)
    overwr = False if write_mode == "append" else True
    parallel = get_key_def('multiprocessing', cfg['tiling'], default=False, expected_type=bool)
    parallel_num_proc = get_key_def('multiprocessing_processes', cfg['tiling'], default=multiprocessing.cpu_count(),
                                    expected_type=int)
    write_dest_raster = get_key_def('write_dest_raster', cfg['tiling'], default=False, expected_type=bool)
    # TODO: why not ask only for a val percentage directly?
    val_percent = int(get_key_def('train_val_percent', cfg['tiling'], default={'val': 0.3})['val'] * 100)
    clahe_clip_limit = get_key_def('clahe_clip_limit', cfg['tiling'], expected_type=Number, default=0)

    attr_field = get_key_def('attribute_field', cfg['dataset'], None, expected_type=str)
    attr_vals = get_key_def('attribute_values', cfg['dataset'], None, expected_type=(Sequence, int))

    # add git hash from current commit to parameters
    with open_dict(cfg):
        cfg.general.git_hash = get_git_hash()

    exp_dir = tiling_root_dir / experiment_name

    if exp_dir.is_dir():
        logging.warning(f'Data path exists: {exp_dir}. Make sure patches belong to the same experiment.')
    Path.mkdir(exp_dir, exist_ok=True, parents=True)

    if debug:
        logging.warning(f'Debug mode activated. Some debug features may mobilize extra disk space and '
                        f'cause delays in execution.')

    src_data_list = aois_from_csv(
        csv_path=csv_file,
        bands_requested=bands_requested,
        attr_field_filter=attr_field,
        attr_values_filter=attr_vals,
        download_data=download_data,
        data_dir=data_dir,
        for_multiprocessing=parallel,
        write_dest_raster=write_dest_raster,
        equalize_clahe_clip_limit=clahe_clip_limit,
    )

    tiler = Tiler(tiling_root_dir=exp_dir,
                  src_aoi_list=src_data_list,
                  patch_size=patch_size,
                  min_annot_perc=min_annot_perc,
                  val_percent=val_percent,
                  write_mode=write_mode,
                  debug=debug,
                  )

    # For each row in csv: (1) tiling imagery and labels
    input_args = []
    tilers = []
    logging.info(f"Preparing patches \n\tSamples_size: {patch_size} ")
    for index, aoi in tqdm(enumerate(tiler.src_aoi_list), position=0, leave=False):
        if aoi.overlap_label_rto_raster == 0.0:
            logging.error(
                f"Features in label file {aoi.label} do not intersect with bounds of raster file "
                f"{aoi.raster.name}, thus this AOI will be skipped.")
            continue
        try:
            tiling_dir = exp_dir / aoi.split.strip() / aoi.aoi_id.strip()
            tiling_dir_img = tiling_dir / 'images'
            tiling_dir_gt = tiling_dir / 'labels' if not tiler.for_inference else None

            if parallel:
                input_args.append([tiler.tiling_per_aoi, aoi, tiling_dir_img, tiling_dir_gt])
            else:
                try:
                    tiler_pair = tiler.tiling_per_aoi(aoi, out_img_dir=tiling_dir_img, out_label_dir=tiling_dir_gt)
                    tilers.append(tiler_pair)
                except ValueError as e:
                    logging.debug(f'Failed to tile\n'
                                  f'Img: {aoi.raster.name}\n'
                                  f'GT: {aoi.label}')
                    raise e

        except OSError as e:
            logging.exception(f'An error occurred while preparing patches with "{Path(aoi.raster.name).stem}" (tiff) and '
                              f'{aoi.label.stem} (gpkg).\n'
                              f'{e}')
            continue

    if parallel:
        logging.info(f'Will proceed to tiling of {len(input_args)} images and labels...')
        with multiprocessing.get_context('spawn').Pool(processes=parallel_num_proc) as pool:
            tilers = pool.map_async(map_wrapper, input_args).get()

    # temporary workaround to support multiprocessing (aois cannot be modified in separate processes)
    # TODO: use mp.Manager() to modify aoi.tiling_pairs_list from within tiling_per_aoi
    tiler.src_aoi_list = []
    for tiled_aoi, rs_tiler_paths, vec_tiler_paths in tqdm(
            tilers, desc="Updating AOIs' information about their patches paths"):
        tiled_aoi.patches_pairs_list = [(rs_ptch, gt_ptch) for rs_ptch, gt_ptch in zip(rs_tiler_paths, vec_tiler_paths)]
        tiler.src_aoi_list.append(tiled_aoi)

    logging.info(f"Tiling done. Creating pixel masks from clipped geojsons...")
    dataset_files = {}
    for dset in tiler.datasets:
        name, _ = tiler.make_dataset_file_name(experiment_name, tiler.min_annot_perc, dset, tiler.attr_vals_exp)
        dset_path = tiler.tiling_root_dir / name
        if dset_path.is_file():
            logging.critical(f'Dataset list exists and will be overwritten: {dset_path}')
            dset_path.unlink()
        dataset_files[dset] = dset_path

    input_args = []
    dataset_lines = []
    datasets_total = {dataset: 0 for dataset in tiler.datasets}
    # loop through line of csv again and
    # (1) filter out training data that doesn't match user-defined conditions such as minimum annotated percent
    # (2) burn filtered labels to raster format
    for aoi in tqdm(tiler.src_aoi_list, position=0,
                    desc='Looping in AOIs'):
        if debug:
            for img_patch, gt_patch in tqdm(
                    aoi.patches_pairs_list,
                    desc='DEBUG: Checking if data patches are valid'):
                try:
                    validate_raster(str(img_patch))
                except Exception as e:
                    logging.error(f'\nInvalid imagery patch: {img_patch}'
                                  f'\n{e}')
                try:
                    check_gdf_load(gt_patch)  # validates ground truth patch
                except Exception as e:
                    logging.error(f'\nInvalid ground truth patch: {img_patch}. '
                                  f'\n{e}')

        for img_patch, gt_patch in tqdm(
                aoi.patches_pairs_list, position=1,
                desc=f'Filter {len(aoi.patches_pairs_list)} patches and burn ground truth'):
            datasets_total[aoi.split] += 1
            # If for inference, write only image patch since there's no ground truth
            if tiler.for_inference:
                dataset_line = f'{Path(img_patch).absolute()}\n'
                dataset_lines.append((aoi.split, dataset_line))
            # if for train, validation or test dataset, then filter, burn and provided complete line to write to file
            else:
                if parallel:
                    input_args.append([tiler.filter_and_burn_dataset, aoi, img_patch, gt_patch])
                else:
                    line_tuple = tiler.filter_and_burn_dataset(aoi,
                                                               img_patch=img_patch,
                                                               gt_patch=gt_patch,
                                                               continuous_vals=continuous_vals,
                                                               save_preview_labels=save_prev_labels)
                    dataset_lines.append(line_tuple)

    if parallel:
        logging.info(f'Parallelizing burning of {len(input_args)} filtered ground truth patches...')
        with multiprocessing.get_context('spawn').Pool(processes=parallel_num_proc) as pool:
            lines = pool.map_async(map_wrapper, input_args).get()
        dataset_lines.extend(lines)

    # write to dataset text file the data that was kept after filtering
    datasets_kept = {dataset: 0 for dataset in tiler.datasets}
    for line_tuple in tqdm(dataset_lines, desc=f"Writing {len(dataset_lines)} lines to dataset files"):
        dataset, dataset_line = line_tuple
        if dataset_line is not None:
            with open(dataset_files[dataset], 'a') as dataset_file:
                dataset_file.write(dataset_line)
                datasets_kept[dataset] += 1

    # final report
    # TODO: write to a file, include aoi-specific stats
    actual_val_ratio = datasets_kept['val'] / (datasets_kept['val'] + datasets_kept['trn'] + 1e-5) * 100
    logging.info(
        f"\nExpected val ratio: {tiler.val_percent} %"
        f"\nActual val ratio: {actual_val_ratio:.2f} %"
    )
    for dataset in tiler.datasets:
        if dataset == 'trn':
            logging.info(f"\nDataset: {dataset}"
                         f"\n\tFilters:"
                         f"\n\t - Minimumum non-zero values (aka minimum annotated percentage): {min_annot_perc}%"
                         f"\n\tKept: {datasets_kept['trn']}"
                         f"\n\tDiscarded: {datasets_total[dataset] - datasets_kept['val'] - datasets_kept['trn']}")
        else:
            logging.info(f"\nDataset: {dataset}"
                         f"\n\tTotal patches: {datasets_kept[dataset]}")
    logging.info(f"\nEnd of process. See tiled dataset lists:")
    for dataset, file in dataset_files.items():
        logging.info(f"{dataset}: {str(file)}")
