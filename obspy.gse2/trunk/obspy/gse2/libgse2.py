#!/usr/bin/env python
#-------------------------------------------------------------------
# Filename: libgse2.py
#  Purpose: Python wrapper for gse_functions of Stefan Stange
#   Author: Moritz Beyreuther
#    Email: moritz.beyreuther@geophysik.uni-muenchen.de
#
# Copyright (C) 2008-2010 Moritz Beyreuther
#---------------------------------------------------------------------
"""
Read & Write Seismograms, Format GSE2.

Python wrappers for gse_functions - The GSE2 library of Stefan Stange.
Currently CM6 compressed GSE2 files are supported, this should be
sufficient for most cases. Gse_functions is written in C and
interfaced via python-ctypes.

See: http://www.orfeus-eu.org/Software/softwarelib.html#gse

GNU Lesser General Public License, Version 3
(http://www.gnu.org/copyleft/lesser.html)
"""

import os
import platform
import ctypes as C
import doctest
import StringIO
import numpy as np
import obspy.core
from obspy.core import UTCDateTime
from obspy.core.util import c_file_p, formatScientific


# Import shared libmseed library depending on the platform.
# XXX: trying multiple names for now - should be removed
if platform.system() == 'Windows':
    if platform.architecture()[0] == '64bit':
        lib_names = ['gse_functions.pyd', '_gse_functions.win64.dll']
    else:
        lib_names = ['gse_functions.pyd', '_gse_functions.win32.dll']
elif platform.system() == 'Darwin':
    lib_names = ['gse_functions.so', '_gse_functions.dylib']
    # 32 and 64 bit UNIX
    #XXX Check glibc version by platform.libc_ver()
else:
    if platform.architecture()[0] == '64bit':
        lib_names = ['gse_functions.so', '_gse_functions.lin64.so']
    else:
        lib_names = ['gse_functions.so', '_gse_functions.so']

# initialize library
lib = None
for lib_name in lib_names:
    try:
        lib = C.CDLL(os.path.join(os.path.dirname(__file__), 'lib',
                                  lib_name))
    except:
        continue
    else:
        break
if not lib:
    msg = 'Could not load shared library "gse_functions" for obspy.gse2.'
    raise ImportError(msg)


class ChksumError(StandardError):
    """
    Exception type for mismatching checksums
    """
    pass


class GSEUtiError(StandardError):
    """
    Exception type for other errors in GSE_UTI
    """
    pass


# gse2 header struct
class HEADER(C.Structure):
    _fields_ = [
        ('d_year', C.c_int),
        ('d_mon', C.c_int),
        ('d_day', C.c_int),
        ('t_hour', C.c_int),
        ('t_min', C.c_int),
        ('t_sec', C.c_float),
        ('station', C.c_char * 6),
        ('channel', C.c_char * 4),
        ('auxid', C.c_char * 5),
        ('datatype', C.c_char * 4),
        ('n_samps', C.c_int),
        ('samp_rate', C.c_float),
        ('calib', C.c_float),
        ('calper', C.c_float),
        ('instype', C.c_char * 7),
        ('hang', C.c_float),
        ('vang', C.c_float),
    ]


# ctypes, PyFile_AsFile: convert python file pointer/ descriptor to C file
# pointer descriptor
C.pythonapi.PyFile_AsFile.argtypes = [C.py_object]
C.pythonapi.PyFile_AsFile.restype = c_file_p

# reading C memory into buffer which can be converted to numpy array
C.pythonapi.PyBuffer_FromMemory.argtypes = [C.c_void_p, C.c_int]
C.pythonapi.PyBuffer_FromMemory.restype = C.py_object

## gse_functions read_header
lib.read_header.argtypes = [c_file_p, C.POINTER(HEADER)]
lib.read_header.restype = C.c_int

## gse_functions decomp_6b
lib.decomp_6b.argtypes = [c_file_p, C.c_int,
                          np.ctypeslib.ndpointer(dtype='int32', ndim=1,
                                                 flags='C_CONTIGUOUS'), ]
lib.decomp_6b.restype = C.c_int

# gse_functions rem_2nd_diff
lib.rem_2nd_diff.argtypes = [np.ctypeslib.ndpointer(dtype='int32', ndim=1,
                                                    flags='C_CONTIGUOUS'),
                             C.c_int]
lib.rem_2nd_diff.restype = C.c_int

# gse_functions check_sum
lib.check_sum.argtypes = [np.ctypeslib.ndpointer(dtype='int32', ndim=1,
                                                 flags='C_CONTIGUOUS'),
                          C.c_int, C.c_int32]
lib.check_sum.restype = C.c_int # do not know why not C.c_int32

# gse_functions buf_init
lib.buf_init.argtypes = [C.c_void_p]
lib.buf_init.restype = C.c_void_p

# gse_functions diff_2nd
lib.diff_2nd.argtypes = [np.ctypeslib.ndpointer(dtype='int32', ndim=1,
                                                flags='C_CONTIGUOUS'),
                         C.c_int, C.c_int]
lib.diff_2nd.restype = C.c_void_p

# gse_functions compress_6b
lib.compress_6b.argtypes = [np.ctypeslib.ndpointer(dtype='int32', ndim=1,
                                                   flags='C_CONTIGUOUS'),
                            C.c_int]
lib.compress_6b.restype = C.c_int

## gse_functions write_header
lib.write_header.argtypes = [c_file_p, C.POINTER(HEADER)]
lib.write_header.restype = C.c_void_p

## gse_functions buf_dump
lib.buf_dump.argtypes = [c_file_p]
lib.buf_dump.restype = C.c_void_p

# gse_functions buf_free
lib.buf_free.argtypes = [C.c_void_p]
lib.buf_free.restype = C.c_void_p

# module wide variable, can be imported by:
# >>> from obspy.gse2 import gse2head
gse2head = [_i[0] for _i in HEADER._fields_]


def isGse2(f):
    pos = f.tell()
    widi = f.read(4)
    f.seek(pos)
    if widi != 'WID2':
        raise TypeError("File is not in GSE2 format")


def writeHeader(f, head):
    """
    Rewriting the write_header Function of gse_functions.c

    Different operating systems are delivering different output for the
    scientific format of floats (fprinf libc6). Here we ensure to deliver
    in a for GSE2 valid format independent of the OS. For speed issues we
    simple cut any number ending with E+0XX or E-0XX down to E+XX or E-XX.
    This fails for numbers XX>99, but should not occur.
    """
    calib = formatScientific("%10.2e" % head.calib)
    header = "WID2 %4d/%02d/%02d %02d:%02d:%06.3f %-5s %-3s %-4s %-3s %8d " + \
             "%11.6f %s %7.3f %-6s %5.1f %4.1f\n"
    f.write(header % (
            head.d_year,
            head.d_mon,
            head.d_day,
            head.t_hour,
            head.t_min,
            head.t_sec,
            head.station,
            head.channel,
            head.auxid,
            head.datatype,
            head.n_samps,
            head.samp_rate,
            calib,
            head.calper,
            head.instype,
            head.hang,
            head.vang))


def read(f, verify_chksum=True):
    """
    Read GSE2 file and return header and data.

    Currently supports only CM6 compressed GSE2 files, this should be
    sufficient for most cases. Data are in circular frequency counts, for
    correction of calper multiply by 2PI and calper: data * 2 * pi *
    header['calper'].

    :type f: File Pointer
    :param f: Open file pointer of GSE2 file to read, opened in binary mode,
              e.g. f = open('myfile','rb')
    :type test_chksum: Bool
    :param verify_chksum: If True verify Checksum and raise Exception if it
                          is not correct
    :rtype: Dictionary, Numpy.ndarray int32
    :return: Header entries and data as numpy.ndarray of type int32.
    """
    fp = C.pythonapi.PyFile_AsFile(f)
    head = HEADER()
    errcode = lib.read_header(fp, C.pointer(head))
    if errcode != 0:
        raise GSEUtiError("Error in lib.read_header")
    data = np.empty(head.n_samps, dtype='int32')
    #import ipdb; ipdb.set_trace()
    n = lib.decomp_6b(fp, head.n_samps, data)
    if n != head.n_samps:
        raise GSEUtiError("Mismatching length in lib.decomp_6b")
    lib.rem_2nd_diff(data, head.n_samps)
    # test checksum only if enabled
    if verify_chksum:
        # calculate checksum from data, as in gse_driver.c line 60
        chksum_data = abs(lib.check_sum(data, head.n_samps, C.c_int32()))
        # find checksum within file
        buf = f.readline()
        chksum_file = -1
        while buf:
            if buf.startswith('CHK2'):
                chksum_file = int(buf.strip().split()[1])
                break
            buf = f.readline()
        if chksum_data != chksum_file:
            msg = "Mismatching checksums, CHK %d != CHK %d"
            raise ChksumError(msg % (chksum_data, chksum_file))
    headdict = {}
    for i in head._fields_:
        headdict[i[0]] = getattr(head, i[0])
    # cleaning up
    del fp, head
    return headdict, data


def write(headdict, data, f, inplace=False):
    """
    Write GSE2 file, given the header and data.

    Currently supports only CM6 compressed GSE2 files, this should be
    sufficient for most cases. Data are in circular frequency counts, for
    correction of calper multiply by 2PI and calper:
    data * 2 * pi * header['calper'].

    Warning: The data are actually compressed in place for performance
    issues, if you still want to use the data afterwards use data.copy()

    :note: headdict dictionary entries C{'datatype', 'n_samps',
           'samp_rate'} are absolutely necessary
    :type data: numpy.ndarray dtype int32
    :param data: Contains the data.
    :type f: File Pointer
    :param f: Open file pointer of GSE2 file to write, opened in binary
              mode, e.g. f = open('myfile','wb')
    :type inplace: Bool
    :param inplace: If True, do compression not on a copy of the data but
                    on the data itself --- note this will change the data
                    values and make them therefore unusable
    :type headdict: Dictionary
    :param headdict: Header containing the following entries::

        'd_year': int,
        'd_mon': int,
        'd_mon': int,
        'd_day': int,
        't_hour': int,
        't_min': int,
        't_sec': float,
        'station': char*6,
        'station': char*6,
        'channel': char*4,
        'auxid': char*5,
        'datatype': char*4,
        'n_samps': int,
        'samp_rate': float,
        'calib': float,
        'calper': float,
        'instype': char*7,
        'hang': float,
        'vang': float
    """
    fp = C.pythonapi.PyFile_AsFile(f)
    n = len(data)
    lib.buf_init(None)
    #
    chksum = C.c_int32()
    chksum = abs(lib.check_sum(data, n, chksum))
    # Maximum values above 2^26 will result in corrupted/wrong data!
    # do this after chksum as chksum does the type checking for numpy array
    # for you
    if not inplace:
        data = data.copy()
    if data.max() > 2 ** 26:
        raise OverflowError("Compression Error, data must be less equal 2^26")
    lib.diff_2nd(data, n, 0)
    ierr = lib.compress_6b(data, n)
    assert ierr == 0, "Error status after compression is NOT 0 but %d" % ierr
    # set some defaults if not available and convert header entries
    headdict.setdefault('datatype', 'CM6')
    headdict.setdefault('vang', -1)
    headdict.setdefault('calper', 1.0)
    headdict.setdefault('calib', 1.0)
    head = HEADER()
    for _i in headdict.keys():
        if _i in gse2head:
            setattr(head, _i, headdict[_i])
    # This is the actual function where the header is written. It avoids
    # the different format of 10.4e with fprintf on Windows and Linux.
    # For further details, see the __doc__ of writeHeader
    writeHeader(f, head)
    lib.buf_dump(fp)
    f.write("CHK2 %8ld\n\n" % chksum)
    lib.buf_free(None)
    del fp, head


def readHead(f):
    """
    Return (and read) only the header of gse2 file as dictionary.

    Currently supports only CM6 compressed GSE2 files, this should be
    sufficient for most cases.

    :type file: File Pointer
    :param file: Open file pointer of GSE2 file to read, opened in binary
                 mode, e.g. f = open('myfile','rb')
    :rtype: Dictionary
    :return: Header entries.
    """
    fp = C.pythonapi.PyFile_AsFile(f)
    head = HEADER()
    lib.read_header(fp, C.pointer(head))
    headdict = {}
    for i in head._fields_:
        headdict[i[0]] = getattr(head, i[0])
    del fp, head
    return headdict


def getStartAndEndTime(f):
    """
    Return start and endtime/date of GSE2 file

    Currently supports only CM6 compressed GSE2 files, this should be
    sufficient for most cases.

    :type f: File Pointer
    :param f: Open file pointer of GSE2 file to read, opened in binary
              mode, e.g. f = open('myfile','rb')
    :rtype: List
    :return: C{[startdate,stopdate,startime,stoptime]} Start and Stop time as
             Julian seconds and as date string.
    """
    fp = C.pythonapi.PyFile_AsFile(f)
    head = HEADER()
    lib.read_header(fp, C.pointer(head))
    seconds = int(head.t_sec)
    microseconds = int(1e6 * (head.t_sec - seconds))
    startdate = UTCDateTime(head.d_year, head.d_mon, head.d_day,
                            head.t_hour, head.t_min, seconds, microseconds)
    stopdate = UTCDateTime(startdate.timestamp +
                           head.n_samps / float(head.samp_rate))
    del fp, head
    return [startdate, stopdate, startdate.timestamp, stopdate.timestamp]


def attach_faked_paz(tr, paz_file, read_digitizer_gain_from_file=False):
    '''
    Attach faked paz_file to tr.stats.paz AttribDict

    This is prototype code. Please use it only if you understand what is
    going on in the source code!

    Attaches a paz AttribDict to trace containing poles zeros and gain. It
    is called faked because we use the overall sensitivity to store also
    the A0_normalization_factor. Which itself is set to 1.0.

    :param tr: An ObsPy trace object
    :param paz_file: path to pazfile or file pointer
    :param read_digitizer_gain_from_file: Experimental, if this option is
            specified, obspy tries to read the digitizer gain from gse2
            attached paz file

    >>> tr = obspy.core.Trace(header={'calib': 0.596})
    >>> f = StringIO.StringIO("""CAL1 RJOB   LE-3D    Z  M24    PAZ 010824 0001
    ... 2
    ... -4.39823 4.48709
    ... -4.39823 -4.48709
    ... 3
    ... 0.0 0.0
    ... 0.0 0.0
    ... 0.0 0.0
    ... 0.4""")
    >>> attach_faked_paz(tr, f)
    >>> print round(tr.stats.paz.sensitivity, -4)
    671140000.0
    '''
    poles = []
    zeros = []
    found_zero = False

    if isinstance(paz_file, str):
        paz_file = open(paz_file, 'r')

    PAZ = paz_file.readlines()
    if PAZ[0][0:4] != 'CAL1':
        raise Exception("Unknown GSE PAZ file")

    ind = 1
    npoles = int(PAZ[ind])
    for i in xrange(npoles):
        try:
            poles.append(complex(*[float(n) for n in PAZ[i+1+ind].split()]))
        except ValueError:
            poles.append(complex(float(PAZ[i+1+ind][:8]), 
                                 float(PAZ[i+1+ind][8:])))

    ind += i + 2
    nzeros = int(PAZ[ind])
    for i in xrange(nzeros):
        try:
            zeros.append(complex(*[float(n) for n in PAZ[i+1+ind].split()]))
        except ValueError:
            zeros.append(complex(float(PAZ[i+1+ind][:8]), 
                                 float(PAZ[i+1+ind][8:])))

    ind += i + 2
    # seismometer_gain / A0_normalization_factor [microVolt/nm/s]
    gain = float(PAZ[ind])

    # remove zero at 0,0j to undo integration in GSE PAZ
    for i, zero in enumerate(list(zeros)):
        if zero == complex(0,0j):
            zeros.pop(i)
            found_zero = True
            break
    if not found_zero:
        raise Exception("Could not remove (0,0j) zero to undo GSE integration")

    tr.stats.paz = obspy.core.AttribDict()
    tr.stats.paz.poles = poles
    tr.stats.paz.zeros = zeros
    # 1000 due to microVolt/nm/s  -> Volt/m/s
    # 1e-6 due to microVolt/count -> Volt/count
    # tr.stats.calib == digitizer_gain [microVolt/count]
    tr.stats.paz.seismometer_gain = gain
    tr.stats.paz.sensitivity = gain * 1000/(tr.stats.calib * 1e-6)
    if read_digitizer_gain_from_file:
        tr.stats.paz.digitizer_gain = float(PAZ[ind+1].split()[-2])
        tr.stats.paz.sensitivity = tr.stats.paz.digitizer_gain * 1000 / \
                (tr.stats.calib * 1e-6)
    # A0_normalization_factor
    tr.stats.paz.gain = 1.0


if __name__ == '__main__':
    doctest.testmod(exclude_empty=True)
