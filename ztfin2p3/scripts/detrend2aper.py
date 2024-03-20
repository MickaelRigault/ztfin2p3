import datetime
import json
import logging
import os
import pathlib
import time
import sys

import numpy as np
import rich_click as click
from astropy.io import fits
from rich.logging import RichHandler
from ztfimg import CCD
from ztfquery.buildurl import filename_to_url

from ztfin2p3 import __version__
from ztfin2p3.aperture import get_aperture_photometry, store_aperture_catalog
from ztfin2p3.io import ipacfilename_to_ztfin2p3filepath
from ztfin2p3.metadata import get_raw
from ztfin2p3.pipe.newpipe import BiasPipe, FlatPipe
from ztfin2p3.science import build_science_image


BIAS_PARAMS = dict(
    corr_nl=True,
    corr_overscan=True,
    axis=0,
    sigma_clip=3,
    mergedhow="nanmean",
    clipping_prop=dict(
        maxiters=1, cenfunc="median", stdfunc="std", masked=False, copy=False
    ),
    get_data_props=dict(overscan_prop=dict(userange=[25, 30])),
)

FLAT_PARAMS = dict(
    corr_nl=True,
    corr_overscan=True,
    axis=0,
    sigma_clip=3,
    mergedhow="nanmean",
    clipping_prop=dict(
        maxiters=1, cenfunc="median", stdfunc="std", masked=False, copy=False
    ),
    get_data_props=dict(overscan_prop=dict(userange=[25, 30])),
)


def daily_datalist(fi):
    # Will probably be implemented in CalibPipe class. Will be cleaner
    datalist = fi.init_datafile.copy()
    datalist["filterid"] = datalist["ledid"]
    for key, items in fi._led_to_filter.items():
        datalist["filterid"] = datalist.filterid.replace(items, key)

    _groupbyk = ["day", "ccdid", "filterid"]
    datalist = datalist.reset_index()
    datalist = datalist.groupby(_groupbyk).ledid.apply(list).reset_index()
    datalist = datalist.reset_index()
    datalist["ledid"] = None
    return datalist


def process_sci(raw_file, flat, bias, newfile_dict):
    logger = logging.getLogger(__name__)
    quads, outs = build_science_image(
        raw_file,
        flat,
        bias,
        dask_level=None,
        corr_nl=True,
        corr_overscan=True,
        overwrite=True,
        fp_flatfield=False,
        newfile_dict=newfile_dict,
        return_sci_quads=True,
        overscan_prop=dict(userange=[25, 30]),
    )

    # If quadrant level :
    aper_stats = {}
    for quad, out in zip(quads, outs):
        # Not using build_aperture_photometry cause it expects
        # filepath and not images. Will change.
        logger.info("aperture photometry for quadrant %d", quad.qid)
        fname_mask = filename_to_url(out, suffix="mskimg.fits.gz", source="local")
        quad.set_mask(fits.getdata(fname_mask))
        apcat = get_aperture_photometry(
            quad,
            cat="gaia_dr2",
            dask_level=None,
            as_path=False,
            minimal_columns=True,
            seplimit=20,
            radius=np.linspace(3, 13),
            bkgann=None,
            joined=True,
            refcat_radius=0.7,
        )
        output_filename = ipacfilename_to_ztfin2p3filepath(
            out, new_suffix=newfile_dict["new_suffix"], new_extension="parquet"
        )
        out = store_aperture_catalog(apcat, output_filename)
        logger.debug(out)
        aper_stats[f"quad_{quad.qid}"] = {
            "quad": quad.qid,
            "naper": len(apcat),
            "file": output_filename,
        }

    return aper_stats


@click.command()
@click.argument("day")
@click.option(
    "-c",
    "--ccdid",
    required=True,
    type=click.IntRange(1, 16),
    help="ccdid in the range 1 to 16",
)
@click.option(
    "--statsdir",
    default=".",
    help="path where statistics are stored",
    show_default=True,
)
@click.option("--suffix", help="suffix for output science files")
@click.option("--force", help="force reprocessing all files?", is_flag=True)
def d2a(day, ccdid, statsdir, suffix, force):
    """Detrending to Aperture pipeline for a given day.

    \b
    Process DAY (must be specified in YYYY-MM-DD format):
    - computer master bias
    - computer master flat
    - for all science exposures, apply master bias and master flat, and run
      aperture photometry.

    """

    logging.basicConfig(
        level="INFO", format="%(message)s", datefmt="[%X]", handlers=[RichHandler()]
    )

    day = day.replace("-", "")
    statsdir = pathlib.Path(statsdir)
    now = datetime.datetime.now(datetime.UTC)
    stats = {"date": now.isoformat(), "day": day, "ccd": ccdid, "version": __version__}
    tot = time.time()

    logger = logging.getLogger(__name__)
    logger.info("processing day %s, ccd=%s", day, ccdid)
    t0 = time.time()
    bi = BiasPipe(day, ccdid=ccdid, nskip=10)
    bi.build_ccds(reprocess=force, **BIAS_PARAMS)
    timing = time.time() - t0
    logger.info("bias done, %.2f sec.", timing)
    stats["bias"] = {"time": timing}

    # Generate flats :
    fi = FlatPipe(day, ccdid=ccdid)
    t0 = time.time()
    fi.build_ccds(bias=bi, reprocess=force, **FLAT_PARAMS)
    timing = time.time() - t0
    logger.info("flat done, %.2f sec.", timing)
    stats["flat"] = {"time": timing}

    # Generate Science :
    # First browse meta data :
    rawsci_list = get_raw("science", fi.period, "metadata", ccdid=ccdid)
    rawsci_list.set_index(["day", "filtercode", "ccdid"], inplace=True)
    rawsci_list = rawsci_list.sort_index()

    bias = bi.get_daily_ccd(day=day, ccdid=ccdid)[day, ccdid]

    newfile_dict = dict(new_suffix=suffix)
    stats["science"] = []
    n_errors = 0

    # iterate over flat filters
    for _, row in flat_datalist.iterrows():
        objects_files = rawsci_list.loc[row.day, row.filterid, row.ccdid]
        nfiles = len(objects_files)
        msg = "processing %s filter=%s ccd=%s: %d files"
        logger.info(msg, row.day, row.filterid, row.ccdid, nfiles)
        sci_info = {
            "day": row.day,
            "filter": row.filterid,
            "ccd": row.ccdid,
            "nfiles": nfiles,
            "files": [],
        }
        flat = CCD.from_data(fi.daily_filter_ccds[row["index"]])

        for i, (_, sci_row) in enumerate(objects_files.iterrows(), start=1):
            raw_file = sci_row.filepath
            logger.info("processing sci %d/%d: %s", i, nfiles, raw_file)
            t0 = time.time()
            try:
                aper_stats = process_sci(raw_file, flat, bias, newfile_dict)
            except Exception as e:
                aper_stats = {}
                status, error_msg = "error", str(e)
                n_errors += 1
                timing = time.time() - t0
                logger.error("sci done, status=%s, %.2f sec.", status, timing)
                logger.error("error was: %s", error_msg)
            else:
                status, error_msg = "ok", ""
                timing = time.time() - t0
                logger.info("sci done, status=%s, %.2f sec.", status, timing)

            sci_info["files"].append(
                {
                    "file": raw_file,
                    "expid": sci_row.expid,
                    "time": timing,
                    "status": status,
                    "error_msg": error_msg,
                    **aper_stats,
                }
            )

        stats["science"].append(sci_info)

    stats["total_time"] = time.time() - tot
    logger.info("all done, %.2f sec.", stats["total_time"])

    stats_file = statsdir / f"stats_{day}_{ccdid}_{now:%Y%M%dT%H%M%S}.json"
    logger.info("writing stats to %s", stats_file)
    stats_file.write_text(json.dumps(stats))

    if n_errors > 0:
        logger.warning("%d sci files failed", n_errors)
        sys.exit(1)
