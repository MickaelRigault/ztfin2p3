
""" library to build the ztfin2p3 pipeline screen flats """
import os
import numpy as np
import dask
import dask.array as da
import warnings
from astropy.io import fits


from ztfimg.base import _Image_, FocalPlane

LED_FILTER = {"zg":[2,3,4,5],
              "zr":[7,8,9,10],
              "zi":[11,12,13],
                }
    
def ledid_to_filtername(ledid):
    """ """
    for f_,v_ in LED_FILTER.items():
        if int(ledid) in v_:
            return f_
    raise ValueError(f"Unknown led with ID {ledid}")



def get_build_datapath(date, ccdid=None, ledid=None, groupby="day"):

    """ """
    # IRSA metadata
    from ..metadata import get_rawmeta
    from ..io import get_filepath
    meta = get_rawmeta("flat", date, ccdid=ccdid, ledid=ledid,  getwhat="filepath", in_meta=True)
    # Parsing out what to do:
    if groupby == "day":
        meta[groupby] = meta.filefracday.astype("str").str[:8]
    elif groupby == "month":
        meta[groupby] = meta.filefracday.astype("str").str[:6]
    else:
        raise ValueError(f"Only groupby day or month implemented: {groupby} given")
    
    datapath = meta.groupby([groupby,"ccdid","ledid"])["filepath"].apply(list).reset_index()
    datapath["filtername"] = datapath["ledid"].apply(ledid_to_filtername)
    datapath["fileout"] = [get_filepath("flat", str(s_[groupby]), 
                            ccdid=int(s_.ccdid), ledid=int(s_.ledid), filtername=s_.filtername)
                           for id_, s_ in datapath.iterrows()]
    return datapath

def build_from_datapath(build_dataframe, assume_exist=False, inclheader=False, overwrite=True, **kwargs):
    """ """
    if not assume_exist:
        from ztfquery import io
        
    outs = []
    for i_, s_ in build_dataframe.iterrows():
        # 
        fileout = s_.fileout
        os.makedirs(os.path.dirname(fileout), exist_ok=True) # build if needed
        files = s_["filepath"]
        if not assume_exist:
            files = io.bulk_get_file(files)
        # 
        bflat = FlatBuilder.from_rawfiles(files, persist=False)
        data, header = bflat.build(set_it=False, inclheader=inclheader, **kwargs)
        output = dask.delayed(fits.writeto)(fileout, data, header=header, overwrite=overwrite)
        outs.append(output)
    return outs

    

class Flat( _Image_ ):
    SHAPE = 6160, 6144
    QUADRANT_SHAPE = 3080, 3072
    def __init__(self, data, header=None, use_dask=True):
        """ """
        _ = super().__init__(use_dask=use_dask)
        self.set_data(data)
        if header is not None:
            self.set_header(header)
            
    # ============== #
    #  I/O           # 
    # ============== #
    @classmethod
    def from_filename(cls, filename, use_dask=True, assume_exist=True):
        """ loads the object given the input file. 

        Parameters
        ----------

        assume_exist: [bool]
            Shall this run ztfquery.io.get_file() ?
            
        """
        from ztfquery import io
        basename = os.path.basename(filename)
        if not basename.startswith("ztfin2p3"):
            filename = io.get_file(filename)

        if ".fits" in basename:
            return cls.read_fits(filename, use_dask=use_dask)
        else:
            raise NotImplementedError(f"Only fits file loader implemented (read_fits) ; {filename} given")

    @classmethod
    def from_date(cls, date, ledid, ccdid, use_dask=True, **kwargs):
        """ """
        from ..io import get_filepath
        filename = get_filepath("flat", date, ccdid=ccdid, ledid=ledid)
        return cls.from_filename(filename, use_dask=use_dask, **kwargs)
        
    
    @classmethod
    def read_fits(cls, fitsfile, use_dask=True):
        """ """
        if use_dask:
            data = da.from_delayed( dask.delayed(fits.getdata)(fitsfile),
                                shape=cls.SHAPE, dtype="float")
            header= dask.delayed(fits.getheader)(fitsfile)
        else:
            data = fits.getdata(fitsfile)
            header= fits.getheader(fitsfile)

        this = cls(data=data, header=header, use_dask=use_dask)
        this._filename = fitsfile
        return this

    @classmethod
    def build_from_rawfiles(cls, rawfiles, **kwargs):
        """ """
        bflat = FlatBuilder.from_rawfiles(rawfiles, persist=False)
        data, header = bflat.build(set_it=False, **kwargs)
        return cls(data, header=None, use_dask=True)

    # ============== #
    #  Method        # 
    # ============== #
    def get_quadrant_data(self, qid, **kwargs):
        """ **kwargs goes to get_data() this then split the data.
        
        Parameters
        ----------
        qid: [int or None/'*']
            which quadrant you want ?
            - int: 1,2,3 or 4
            - None or '*'/'all': all quadrant return as list [1,2,3,4]

        **kwargs goes to get_data()

        Returns
        -------
        ndarray (numpy or dask)
        """
        if qid in ["*","all"]:
            qid = None
        if qid is not None:
            qid = int(qid)
            
        dataccd = self.get_data(**kwargs)
        # this accounts for all rotation and rebin did before
        qshape = np.asarray(np.asarray(dataccd.shape)/2, dtype="int")

        if qid == 1:
            data_ = dataccd[qshape[0]:, qshape[1]:]
        elif qid == 2:
            data_ = dataccd[qshape[0]:, :qshape[1]]
        elif qid == 3:
            data_ = dataccd[:qshape[0], :qshape[1]]
        elif qid == 4:
            data_ = dataccd[:qshape[0], qshape[1]:]
        elif qid is None or qid in ["*","all"]:
            data_ = [dataccd[qshape[0]:, qshape[1]:],
                     dataccd[qshape[0]:, :qshape[1]],
                     dataccd[:qshape[0], :qshape[1]],
                     dataccd[:qshape[0], qshape[1]:]
                    ]
            
        else:
            raise ValueError(f"qid must be 1->4 {qid} given")
        
        return data_


class FlatFocalPlane( FocalPlane ):
    
    @classmethod
    def from_filenames(cls, flatfilenames, use_dask=True, **kwargs):
        """ """
        this = cls(use_dask=use_dask)
        for file_ in flatfilenames:
            ccd_ = Flat.from_filename(file_, use_dask=use_dask, **kwargs)
            ccdid = int(file_.split("_")[-3].replace("c",""))
            this.set_ccd(ccd_, ccdid=ccdid)

        this._filenames = flatfilenames
        return this
    
    @classmethod
    def from_date(cls, date, ledid, use_dask=True, **kwargs):
        """ """
        from ..io import get_filepath
        ccdids = np.arange(1,17)
        filenames = [get_filepath("flat", date, ccdid=ccdid_, ledid=ledid)
                     for ccdid_ in ccdids]
        return cls.from_filenames(filenames, use_dask=use_dask, **kwargs)
    
    # ============= # 
    #   Methods     #
    # ============= #
    def get_quadrant_data(self, rcid, **kwargs):
        """ """
        ccdid, qid = self.rcid_to_ccdid_qid(rcid)
        return self.get_ccd(ccdid).get_quadrant_data(qid, **kwargs)
    
    def get_quadrant(self, *args, **kwargs):
        """ """
        raise NotImplemented("get_quadrant() is not usable as flat are CCD-base. See get_quadrant_data().")
    
    
# ==================== #
#                      #
#   Flat Builder       #
#                      #
# ==================== #
from .builder import CalibrationBuilder
class FlatBuilder( CalibrationBuilder ): 
    # -------- # 
    # BUILDER  #
    # -------- #
    def build(self, corr_nl=True, corr_overscan=True, clipping=True,
                  set_it=False, inclheader=True, **kwargs):
        """ """
        return super().build(corr_nl=corr_nl,
                                 corr_overscan=corr_overscan,
                                 clipping=clipping,
                                 set_it=set_it, inclheader=inclheader,
                                 **kwargs)

    def build_header(self, keys=None, refid=0, inclinput=False):
        """ """
        from astropy.io import fits

        if keys is None:
            keys = ["ORIGIN","OBSERVER","INSTRUME","IMGTYPE","EXPTIME",
                    "CCDSUM","CCD_ID","CCDNAME","PIXSCALE","PIXSCALX","PIXSCALY",
                    "FRAMENUM","ILUM_LED", "ILUMWAVE", "PROGRMID","FILTERID",
                    "FILTER","FILTPOS","RA","DEC", "OBSERVAT"]

        header = self.imgcollection.get_singleheader(refid, as_serie=True)
        if type(header) == dask.dataframe.core.Series:
            header = header.compute()

        header = header.loc[keys]
        
        newheader = fits.Header(header.loc[keys].to_dict())
        newheader.set(f"NINPUTS",self.imgcollection.nimages, "num. input images")
        
        if inclinput:
            basenames = self.imgcollection.filenames
            for i, basename_ in enumerate(basenames):
                newheader.set(f"INPUT{i:02d}",basename_, "input image")
            
        return newheader
    
