#!/usr/bin/env python3
"""
Filtered single scene interferogram stack generator.
"""

import os
import sys
import re
import traceback
import argparse
import json
import logging
import hashlib
import shutil
import pickle
import h5py
from subprocess import check_call
from datetime import datetime
from glob import glob

from giant_time_series.filt import filter_ifgs
from giant_time_series.utils import (get_envelope, dataset_exists, call_noerr,
write_dataset_json)

import celeryconfig as conf


log_format = "[%(asctime)s: %(levelname)s/%(name)s/%(funcName)s] %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)
logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])


BASE_PATH = os.path.dirname(__file__)


ID_TMPL = "filtered-ifg-stack_{sensor}-TN{track}-{startdt}Z-{enddt}Z-{hash}-{version}"
TN_RE = re.compile(r'_TN(\d+)_')
DATASET_VERSION = "v0.1"


# read in example.rsc template
with open(os.path.join(BASE_PATH, "example.rsc.tmpl")) as f:
    RSC_TMPL = f.read()


# read in prepdataxml.py template
with open(os.path.join(BASE_PATH, "prepdataxml.py.tmpl")) as f:
    PREPDATA_TMPL = f.read()


# read in prepsbasxml.py template
with open(os.path.join(BASE_PATH, "prepsbasxml.py.tmpl")) as f:
    PREPSBAS_TMPL = f.read()


def main(input_json_file):
    """Main stack generator."""

    # save cwd (working directory)
    cwd = os.getcwd()

    # get time-series input
    input_json_file = os.path.abspath(input_json_file)
    if not os.path.exists(input_json_file):
        raise RuntimeError("Failed to find %s." % input_json_file)
    with open(input_json_file) as f:
        input_json = json.load(f)
    logger.info("input_json: {}".format(json.dumps(input_json, indent=2)))

    # get project
    project = input_json['project']

    # get ifg products
    products = input_json['products']

    # get region of interest
    if input_json['region_of_interest']:
        logger.info("Running Time Series with Region of Interest")
        min_lat, max_lat, min_lon, max_lon = input_json['region_of_interest']
    else:
        logger.info("Running Time Series on full data")
        min_lon, max_lon, min_lat, max_lat = get_envelope(input_json['products'])
        logger.info("env: {} {} {} {}".format(min_lon, max_lon, min_lat, max_lat))

    # get reference point in radar coordinates and length/width for box
    ref_lat, ref_lon = input_json['ref_point']
    ref_width = int((input_json['ref_box_num_pixels'][0]-1)/2)
    ref_height = int((input_json['ref_box_num_pixels'][1]-1)/2)

    # get coverage threshold
    covth = input_json['coverage_threshold']

    # get coherence threshold
    cohth = input_json['coherence_threshold']

    # get range and azimuth pixel size
    range_pixel_size = input_json['range_pixel_size']
    azimuth_pixel_size = input_json['azimuth_pixel_size']

    # get incidence angle
    inc = input_json['inc']

    # get filt
    filt = input_json['filt']

    # network and gps deramp
    netramp = input_json['netramp']
    gpsramp = input_json['gpsramp']

    # get subswath
    if isinstance(input_json['subswath'], list): 
        subswath = input_json['subswath']
    else: subswath = [ input_json['subswath'] ]
    subswath = [ int(i) for i in subswath ]
    input_json['subswath'] = subswath

    # filter interferogram stack
    filt_info = filter_ifgs(products, min_lat, max_lat, min_lon, max_lon,
                            ref_lat, ref_lon, ref_width, ref_height, covth,
                            cohth, range_pixel_size, azimuth_pixel_size,
                            inc, filt, netramp, gpsramp, subswath)

    # dump filter info
    with open('filt_info.pkl', 'wb') as f:
        pickle.dump(filt_info, f)

    # get info
    center_lines_utc = filt_info['center_lines_utc']
    ifg_info = filt_info['ifg_info']
    ifg_coverage = filt_info['ifg_coverage']

    # print status after filtering
    logger.info("After filtering: {} out of {} will be used for GIAnT processing".format(
                len(ifg_info), len(products)))

    # croak no products passed filters
    if len(ifg_info) == 0:
        raise RuntimeError("All products in the stack were filtered out. Check thresholds.")

    # get sorted ifg date list
    ifg_list = sorted(ifg_info)

    # get track number
    track = int(TN_RE.search(ifg_info[ifg_list[0]]['product']).group(1))
    logger.info("Track: {}".format(track))

    # get sensor
    sensor = ifg_info[ifg_list[0]]['sensor']
    sensor_name = ifg_info[ifg_list[0]]['sensor_name']
    logger.info("Sensor: {}".format(sensor))
    logger.info("Sensor name: {}".format(sensor_name))

    # get platforms
    platform = set()
    for i in ifg_list:
        p = ifg_info[i]['platform']
        logger.info("platform for {}: {}".format(i, p))
        if p is not None:
            if not isinstance(p, list): p = [ p ]
            platform = platform.union(p)
    platform = list(platform)

    # get endpoint configurations
    es_url = conf.GRQ_ES_URL
    es_index = conf.DATASET_ALIAS
    logger.info("GRQ url: {}".format(es_url))
    logger.info("GRQ index: {}".format(es_index))

    # get hash of all params
    m = hashlib.new('md5')
    m.update("{} {} {} {}".format(min_lon, max_lon, min_lat, max_lat).encode('utf-8'))
    m.update("{} {}".format(*input_json['ref_point']).encode('utf-8'))
    m.update("{} {}".format(*input_json['ref_box_num_pixels']).encode('utf-8'))
    m.update("{}".format(cohth).encode('utf-8'))
    m.update("{}".format(range_pixel_size).encode('utf-8'))
    m.update("{}".format(azimuth_pixel_size).encode('utf-8'))
    m.update("{}".format(inc).encode('utf-8'))
    m.update("{}".format(netramp).encode('utf-8'))
    m.update("{}".format(gpsramp).encode('utf-8'))
    m.update("{}".format(filt).encode('utf-8'))
    m.update("{}".format(track).encode('utf-8'))
    m.update("{}".format(sensor).encode('utf-8'))
    m.update(" ".join(platform).encode('utf-8'))
    m.update(" ".join(ifg_list).encode('utf-8'))
    roi_ref_hash = m.hexdigest()[0:5]

    # get time series product ID
    center_lines_utc.sort()
    id = ID_TMPL.format(sensor=sensor, track=track,
                        startdt=center_lines_utc[0].strftime('%Y%m%dT%H%M%S'),
                        enddt=center_lines_utc[-1].strftime('%Y%m%dT%H%M%S'),
                        hash=roi_ref_hash, version=DATASET_VERSION)
    logger.info("Product ID for version {}: {}".format(DATASET_VERSION, id))

    # check if time-series already exists
    if dataset_exists(es_url, es_index, id):
        logger.info("{} was previously generated and exists in GRQ database.".format(id))
        sys.exit(0)

    # write ifg.list
    with open ('ifg.list', 'w') as f:
        for i, dt_id in enumerate(ifg_list):
            logger.info("{start_dt} {stop_dt} {bperp:7.2f} {sensor} {width} {length} {wavelength} {heading_deg} {center_line_utc} {xlim} {ylim} {rxlim} {rylim}\n".format(**ifg_info[dt_id]))
            f.write("{start_dt} {stop_dt} {bperp:7.2f} {sensor}\n".format(**ifg_info[dt_id]))

            # write input files on first ifg
            if i == 0:
                # write example.rsc
                with open('example.rsc', 'w') as g:
                    g.write(RSC_TMPL.format(**ifg_info[dt_id]))

                # write prepdataxml.py
                with open('prepdataxml.py', 'w') as g:
                    g.write(PREPDATA_TMPL.format(**ifg_info[dt_id]))

                # write prepsbasxml.py
                with open('prepsbasxml.py', 'w') as g:
                    g.write(PREPSBAS_TMPL.format(nvalid=len(ifg_list), **ifg_info[dt_id]))

    # copy userfn.py
    shutil.copy(os.path.join(BASE_PATH, "userfn.py"), "userfn.py")

    # create data.xml
    logger.info("Running step 1: prepdataxml.py")
    check_call("python2 prepdataxml.py", shell=True)

    # prepare interferogram stack
    logger.info("Running step 2: PrepIgramStack.py")
    check_call("{}/PrepIgramStackWrapper.py".format(BASE_PATH), shell=True)

    # create sbas.xml
    logger.info("Running step 3: prepsbasxml.py")
    check_call("python2 prepsbasxml.py", shell=True)

    # stack preprocessing: apply atmospheric corrections and estimate residual orbit errors
    logger.info("Running step 4: ProcessStack.py")
    check_call("{}/ProcessStackWrapper.py".format(BASE_PATH), shell=True)

    # extract timestep dates
    proc_stack = os.path.join(cwd, 'Stack', 'PROC-STACK.h5')
    h5f = h5py.File(proc_stack, 'r')
    times = h5f.get('dates')[:]
    h5f.close()
    timesteps = [datetime.fromordinal(int(i)).isoformat('T') for i in times[:]]

    # create product directory
    prod_dir = id
    os.makedirs(prod_dir, 0o755)

    # move and compress HDF5 products
    prod_files = glob("Stack/*")
    for i in prod_files:
        shutil.move(i, prod_dir)
        check_call("pigz -f -9 {}".format(os.path.join(prod_dir, os.path.basename(i))), shell=True)

    # create browse image
    png_files = glob("Figs/Igrams/*.png")
    shutil.copyfile(png_files[0], os.path.join(prod_dir, "browse.png"))
    call_noerr("convert -resize 250x250 {} {}".format(png_files[0],
               os.path.join(prod_dir, "browse_small.png")))

    # copy pngs
    for i in png_files: shutil.move(i, prod_dir)

    # save other files to product directory
    shutil.copyfile(input_json_file, os.path.join(prod_dir,"{}.context.json".format(id)))
    shutil.move("filt_info.pkl", prod_dir)
    shutil.move("data.xml", prod_dir)
    shutil.move("example.rsc", prod_dir)
    shutil.move("ifg.list", prod_dir)
    shutil.move("prepdataxml.py", prod_dir)
    shutil.move("prepsbasxml.py", prod_dir)
    shutil.move("sbas.xml", prod_dir)
    shutil.move("userfn.py", prod_dir)

    # create met json
    met = {
        "bbox": [
            [ max_lat, max_lon ],
            [ max_lat, min_lon ],
            [ min_lat, min_lon ],
            [ min_lat, max_lon ],
            [ max_lat, max_lon ],
        ],
        "dataset_type": "ifg-stack",
        "product_type": "ifg-stack",
        "reference": False,
        "sensing_time_initial": timesteps[0],
        "sensing_time_final": timesteps[-1],
        "sensor": sensor_name,
        "platform": platform,
        "spacecraftName": platform,
        "tags": [ input_json['project'] ],
        "trackNumber": track,
        "swath": input_json['subswath'],
        "ifg_count": len(ifg_info),
        "ifgs": [ifg_info[i]['product'] for i in sorted(ifg_info)],
        "timestep_count": len(timesteps),
        "timesteps": timesteps,
    }
    met_file = os.path.join(prod_dir, "{}.met.json".format(id))
    with open(met_file, 'w') as f:
        json.dump(met, f, indent=2)

    # create dataset json
    geojson_bbox = [
        [ max_lon, max_lat ],
        [ max_lon, min_lat ],
        [ min_lon, min_lat ],
        [ min_lon, max_lat ],
        [ max_lon, max_lat ],
    ]
    write_dataset_json(prod_dir, id, geojson_bbox, timesteps[0], timesteps[-1], DATASET_VERSION)

    # clean out SAFE directories and symlinks
    for i in input_json['products']: shutil.rmtree(i)
    for i in ifg_list: os.unlink(i)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_json_file", help="input JSON file")
    args = parser.parse_args()
    try: main(args.input_json_file)
    except Exception as e:
        with open('_alt_error.txt', 'w') as f:
            f.write("%s\n" % str(e))
        with open('_alt_traceback.txt', 'w') as f:
            f.write("%s\n" % traceback.format_exc())
        raise
    sys.exit(0)