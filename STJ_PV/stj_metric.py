# -*- coding: utf-8 -*-
"""Calculate the position of the subtropical jet in both hemispheres."""
import subprocess
import yaml
import numpy as np
import numpy.polynomial as poly
from scipy import signal as sig
from scipy.signal import argrelextrema

from netCDF4 import num2date, date2num
import pandas as pd
import xarray as xr
from xarray import ufuncs as xu
from STJ_PV import utils

try:
    from eddy_terms import Kinetic_Eddy_Energies
except ModuleNotFoundError:
    print('Eddy Terms Function not available, STJKangPolvani not available')

try:
    GIT_ID = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
except subprocess.CalledProcessError:
    GIT_ID = 'NONE'


class STJMetric:
    """
    Generic Class containing Sub Tropical Jet metric methods and attributes.

    Parameters
    ----------
    name : string
        Name of this type of method
    data : :class:`~STJ_PV.input_data.InputData`
        Object containing required input data
    props : :class:`~STJ_PV.run_stj.JetFindRun`
        Properties about the current jet finding attempt, including log file

    """

    def __init__(self, name=None, data=None, props=None):
        """Initialize a subtropical jet metric."""
        self.name = name
        self.data = data
        self.props = props.config
        self.log = props.log
        self.out_data = {}
        self.time = None
        self.hemis = None
        self.debug_data = {}
        self.plot_idx = 0

    def _drop_vars(self, out_var):
        """Drop coordinate variables that may not match."""
        for drop_var in ['pv', self.data.cfg['lat']]:
            if drop_var in self.out_data[out_var].coords:
                self.out_data[out_var] = self.out_data[out_var].drop(drop_var)

    def save_jet(self):
        """Save jet position to file."""
        # Setup metadata for output variables
        props = {
            'lat': {
                'standard_name': 'jet_latitude',
                'descr': 'Latitude of subtropical jet',
                'units': 'degrees_north',
            },
            'intens': {
                'standard_name': 'jet_intensity',
                'descr': 'Intensity of subtropical jet',
                'units': 'm s-1',
            },
            'theta': {
                'standard_name': 'jet_theta',
                'descr': 'Theta level of subtropical jet',
                'units': 'K',
            },
        }

        for out_var in self.out_data:
            # Clean up dimension labels
            self._drop_vars(out_var)

            prop_name = out_var.split('_')[0]
            self.out_data[out_var] = self.out_data[out_var].assign_attrs(props[prop_name])

        out_dset = xr.Dataset(self.out_data)
        self.log.info("WRITE TO {output_file}".format(**self.props))
        file_attrs = {'commit-id': GIT_ID, 'run_props': yaml.safe_dump(self.props)}
        out_dset = out_dset.assign_attrs(file_attrs)
        out_dset.to_netcdf(self.props['output_file'] + '.nc')

    def set_hemis(self, shemis):
        r"""
        Select hemisphere data.

        This function sets `self.hemis` to be an length N list of slices such that only
        the desired hemisphere is selected with N-D data (e.g. uwind and ipv) along all
        other axes. It also returns the latitude for the selected hemisphere, an index
        to select the hemisphere in output arrays, and the extrema function to find
        min/max of PV derivative in a particular hemisphere.

        Parameters
        ----------
        shemis : boolean
            If true - use southern hemisphere data, if false, use NH data

        Returns
        -------
        extrema : function
            Function used to identify extrema in meridional PV gradient, either
            :func:`scipy.signal.argrelmax` if SH, or :func:`scipy.signal.argrelmin`
            for NH
        lats : tuple
            (start, end) of latitude, for use with
            `xarray.DataArray.sel(lat=slice(\*lats))`
        hem_s : string
            Abbreviation for hemisphere (NH or SH)

        """
        lats = [self.props.get('min_lat', 0), self.props.get('max_lat', 90)]
        lat_dec = self.data[self.data.cfg['lat']][0] > self.data[self.data.cfg['lat']][-1]

        if shemis:
            _lstart = -90
            _lend = 0
            extrema = sig.argrelmax
            hem_s = 'sh'
            if lats[0] >= 0 and lats[1] >= 0:
                # Lats are positive, multiply by -1 to get positive for SH
                lats = (-lats[0], -lats[1])
        else:
            _lstart = 0
            _lend = 90
            extrema = sig.argrelmin
            hem_s = 'nh'
            if lats[0] <= 0 and lats[1] <= 0:
                # Lats are negative, multiply by -1 to get positive for NH
                lats = (-lats[0], -lats[1])

        if lat_dec:
            _lstart, _lend = _lend, _lstart

        self.hemis = {self.data.cfg['lat']: slice(_lstart, _lend)}
        return extrema, lats, hem_s

    def compute(self):
        """Compute all dask arrays in `self.out_data`."""
        for vname in self.out_data:
            self._drop_vars(vname)
            try:
                self.out_data[vname] = self.out_data[vname].compute()
            except (AttributeError, TypeError):
                self.props.log.info(
                    'Compute fail data at %s not dask array it is %s',
                    vname,
                    type(self.out_data[vname]),
                )

    def append(self, other):
        """Append another metric's intensity, latitude, and theta positon to this one."""
        for var_name in self.out_data:
            self.out_data[var_name] = xr.concat(
                (self.out_data[var_name], other.out_data[var_name]),
                dim=self.data.cfg['time'],
            )


class STJPV(STJMetric):
    """
    Subtropical jet position metric using dynamic tropopause on isentropic levels.

    Parameters
    ----------
    props : :class:`~STJ_PV.run_stj.JetFindRun`
        Class containing properties about the current search for the STJ
    data : :class:`~STJ_PV.input_data.InputData`
        Input data class containing a year (or more) of required data

    """

    def __init__(self, props, data):
        """Initialise Metric using PV Gradient Method."""
        name = 'PVGrad'
        super(STJPV, self).__init__(name=name, props=props, data=data)
        # Some config options should be properties for ease of access
        self.pv_lev = self.props['pv_value']
        self.fit_deg = self.props['fit_deg']
        self.min_lat = self.props['min_lat']

        if self.props['poly'].lower() in ['cheby', 'cby', 'cheb', 'chebyshev']:
            self.pfit = poly.chebyshev.chebfit
            self.pder = poly.chebyshev.chebder
            self.peval = poly.chebyshev.chebval

        elif self.props['poly'].lower() in ['leg', 'legen', 'legendre']:
            self.pfit = poly.legendre.legfit
            self.pder = poly.legendre.legder
            self.peval = poly.legendre.legval

        elif self.props['poly'].lower() in ['poly', 'polynomial']:
            self.pfit = poly.polynomial.polyfit
            self.pder = poly.polynomial.polyder
            self.peval = poly.polynomial.polyval

        # Initialise latitude & theta output dicts
        self.out_data = {}

    def _poly_deriv(self, lat, data, deriv=1):
        """
        Calculate the `deriv`^th derivative of a one-dimensional array w.r.t. latitude.

        Parameters
        ----------
        data : array_like
            1D array of data, same shape as `self.data.lat`
        y_s, y_e : integers, optional
            Start and end indices of subset, default is None
        deriv : integer, optional
            Number of derivatives of `data` to take

        Returns
        -------
        poly_der : array_like
            1D array of 1st derivative of data w.r.t. latitude between indices y_s and y_e

        """
        # Determine where data is valid...Intel's Lin Alg routines fail when trying to do
        # a least squares fit on array with np.nan, use where it's valid to do the fit
        valid = np.isfinite(data)
        try:
            poly_fit = self.pfit(lat[valid], data[valid], self.fit_deg)
        except TypeError as err:
            # This can happen on fitting the polynomial:
            # `raise TypeError("expected non-empty vector for x")`
            # If that's the error we get, just set the position to 0,
            # which is later masked, otherwise raise the error
            if 'non-empty' in err.args[0]:
                poly_fit = np.zeros(self.fit_deg)
            else:
                raise

        poly_der = self.peval(lat, self.pder(poly_fit, deriv))

        return poly_der, (poly_fit, lat[valid])

    def isolate_pv(self, pv_lev):
        """
        Get the potential temperature, zonal wind and zonal wind shear for a PV level.

        Parameters
        ----------
        pv_lev : float
            PV value (for a particular hemisphere, >0 for NH, <0 for SH) on which to
            interpolate potential temperature and wind
        theta_bnds : tuple, optional
            Start and end theta levels to use for interpolation. Default is None,
            if None, use all theta levels, otherwise restrict so
            theta_bnds[0] <= theta <= theta_bnds[1]

        Returns
        -------
        theta_xpv : array_like
            N-1 dimensional array (where `self.data.ipv` is N-D) of potential temperature
            on `pv_lev` PVU
        uwnd_xpv : array_like
            N-1 dimensional array (where `self.data.uwnd` is N-D) of zonal wind
            on `pv_lev` PVU
        ushear : array_like
            Wind shear between uwnd_xpv and "surface", meaning the lowest valid level

        """
        if 'theta_s' in self.data.cfg and 'theta_e' in self.data.cfg:
            theta_bnds = (self.data.cfg['theta_s'], self.data.cfg['theta_e'])
            assert theta_bnds[0] < theta_bnds[1], 'Start level not strictly less than end'
            theta_bnds = slice(*theta_bnds)
        else:
            theta_bnds = slice(None)

        lev_name = self.data.cfg['lev']
        lev_subset = {lev_name: theta_bnds}

        # Create a copy of the level subset so we can add the latitude hemisphere
        # subset to do both at once on the 4D arrays, without interfering with the 1D sel
        # because if the 'lat' dim isn't a dim on the self.data[lev_name] array, the
        # selection will raise an error
        _latlev = lev_subset.copy()
        _latlev.update(self.hemis)
        _pv = self.data.ipv.sel(**_latlev).load()
        _uwnd = self.data.uwnd.sel(**_latlev).load()
        self.log.info('     COMPUTING THETA ON %.1e', pv_lev)
        theta_xpv = utils.xrvinterp(
            self.data[lev_name].sel(**lev_subset),
            _pv,
            pv_lev,
            levname=lev_name,
            newlevname='pv',
        ).load()

        self.log.info('     COMPUTING UWND ON %.1e', pv_lev)
        uwnd_xpv = utils.xrvinterp(
            _uwnd, _pv, pv_lev, levname=lev_name, newlevname='pv'
        ).load()

        self.log.info('     COMPUTING SHEAR FROM %.1e', pv_lev)
        ushear = self._get_max_shear(uwnd_xpv.squeeze(dim='pv')).load()

        return theta_xpv.squeeze(dim='pv'), uwnd_xpv.squeeze(dim='pv'), ushear

    def find_jet(self, shemis=True, debug=False):
        """
        Find the subtropical jet using input parameters.

        Parameters
        ----------
        shemis : logical, optional
            If True, find jet position in Southern Hemisphere,
            If False, find N.H. jet
        debug : logical, optional
            Enter debug mode if true, returns d(theta) / d(lat) values,
            polynomial fit, and jet latitude

        """
        if shemis and self.pv_lev < 0 or not shemis and self.pv_lev > 0:
            pv_lev = np.array([self.pv_lev]) * 1e-6
        else:
            pv_lev = -1 * np.array([self.pv_lev]) * 1e-6

        extrema, lats, hem_s = self.set_hemis(shemis)
        self.log.info('COMPUTING THETA/UWND ON %.1f PVU', pv_lev * 1e6)
        # Get theta on PV==pv_level
        theta_xpv, uwnd_xpv, ushear = self.isolate_pv(pv_lev)

        # Shortcut for latitude variable name, since it's used a lot
        vlat = self.data.cfg['lat']

        # Restrict theta and shear between our min / max latitude from config file
        # that was processed by self.set_hemis
        _theta = theta_xpv.sel(**{vlat: slice(*lats)})
        if _theta[vlat].shape[0] == 0:
            # If the selection is empty along the latitude axis, that means
            # the selection is the wrong way around, so flip it before moving on
            lats = lats[::-1]
            _theta = theta_xpv.sel(**{vlat: slice(*lats)})
        _shear = ushear.sel(**{vlat: slice(*lats)})

        self.log.info('COMPUTING JET POSITION FOR %s in %d', hem_s, self.data.year)
        # Set up computation of all the jet latitudes at once using self.find_single_jet
        # The input_core_dims is a list of lists, that tells xarray/dask that the
        # arguments _theta, _theta.lat, and _shear are passed to self.find_single_jet
        # with that dimension intact. The kwargs argument passes keyword args to the
        # self.find_single_jet
        if not debug:
            jet_lat = xr.apply_ufunc(
                self.find_single_jet,
                _theta,
                _theta[vlat],
                _shear,
                input_core_dims=[[vlat], [vlat], [vlat]],
                vectorize=True,
                dask='parallelized',
                kwargs={'extrema': extrema},
            )
        else:
            dtheta, theta_fit, jet_lat = self._debug_jet_loop(_theta, _shear, extrema)

        # Select the data for level and intensity by the latitudes generated
        jet_theta = theta_xpv.sel(**{vlat: jet_lat})
        jet_intens = uwnd_xpv.sel(**{vlat: jet_lat})

        # This masks our xarrays of intrest where the jet_lat == 0.0, which is set
        # whenever there is invalid data for a particular cell
        jet_intens = jet_intens.where(jet_lat != 0.0)
        jet_theta = jet_theta.where(jet_lat != 0.0)
        jet_lat = jet_lat.where(jet_lat != 0.0)

        # If we're interested in mean / median, take those
        if self.props['zonal_opt'].lower() == 'mean':
            jet_intens = jet_intens.mean(dim=self.data.cfg['lon'])
            jet_theta = jet_theta.mean(dim=self.data.cfg['lon'])
            jet_lat = jet_lat.mean(dim=self.data.cfg['lon'])

        elif self.props['zonal_opt'].lower() == 'median':
            jet_intens = jet_intens.median(dim=self.data.cfg['lon'])
            jet_theta = jet_theta.median(dim=self.data.cfg['lon'])
            jet_lat = jet_lat.median(dim=self.data.cfg['lon'])

        # Put the parameters into place for this hemisphere
        self.out_data['intens_{}'.format(hem_s)] = jet_intens
        self.out_data['theta_{}'.format(hem_s)] = jet_theta
        self.out_data['lat_{}'.format(hem_s)] = jet_lat

        if debug:
            output = dtheta, theta_fit, _theta, jet_lat
        else:
            output = None

        return output

    def _debug_jet_loop(self, _theta, _shear, extrema):
        """Loop over each time/lon in _theta rather than xarray.apply_ufunc."""
        dims = _theta.shape
        tht_fit_shape = (self.props['fit_deg'] + 1, dims[0], dims[-1])
        dtheta = np.zeros(dims)
        theta_fit = np.zeros(tht_fit_shape)
        jet_lat = np.zeros((dims[0], dims[-1]))

        lat = _theta[self.data.cfg['lat']].values

        dims_names = (self.data.cfg['time'], self.data.cfg['lon'])
        coords = {dim_name: _theta[dim_name] for dim_name in dims_names}

        _theta = _theta.compute()
        _shear = _shear.compute()

        for tix in range(dims[0]):
            for xix in range(dims[-1]):
                # Find derivative of dynamical tropopause
                _info = self.find_single_jet(
                    _theta[tix, :, xix].values,
                    lat,
                    _shear[tix, :, xix].values,
                    extrema,
                    debug=True,
                )
                jet_lat[tix, xix] = _info[0]
                dtheta[tix, :, xix] = _info[2]
                theta_fit[:, tix, xix] = _info[3][0]

        jet_lat = xr.DataArray(jet_lat, coords=coords, dims=dims_names)

        _dims = (self.data.cfg['time'], self.data.cfg['lat'], self.data.cfg['lon'])
        _coords = {dim_name: _theta[dim_name] for dim_name in _dims}
        dtheta = xr.DataArray(dtheta, coords=_coords, dims=_dims)

        _dims = (_dims[0], _dims[-1])
        _coords = {dim_name: _theta[dim_name] for dim_name in _dims}
        _coords['deg'] = np.arange(theta_fit.shape[0])
        _dims = ('deg', *_dims)
        theta_fit = xr.DataArray(theta_fit, coords=_coords, dims=_dims)

        return dtheta, theta_fit, jet_lat

    def _get_max_shear(self, uwnd_xpv):
        """Get maximum wind-shear between surface and PV surface."""
        # Our zonal wind data is on isentropic levels. Lower levels are bound to be below
        # the surface in some places, so we need to use the lowest valid wind level as
        # the surface, so do some magic to make that happen.
        _lev = self.data.cfg['lev']
        if self.data[_lev].shape[0] != self.data.chunks[_lev][0]:
            # re-chunk wind to ensure continuity along vertical axis
            self.data = self.data.chunk({_lev: -1})

        uwnd_sfc = xr.apply_ufunc(
            lowest_valid,
            self.data.uwnd,
            input_core_dims=[[self.data.cfg['lev']]],
            vectorize=True,
            dask='parallelized',
            output_dtypes=[float],
        )

        return uwnd_xpv - uwnd_sfc.sel(**self.hemis)

    def find_single_jet(self, theta_xpv, lat, ushear, extrema, debug=False):
        """
        Find jet location for a 1D array of theta on latitude.

        Parameters
        ----------
        theta_xpv : array_like
            Theta on PV level as a function of latitude
        lat : array_like
            1D array of latitude same shape as theta_xpv from :py:meth:`~isolate_pv`
        ushear : array_like
            1D array along latitude axis of maximum surface - troposphere u-wind shear
        debug : boolean
            If True, returns debugging information about how jet position is found,
            if False (default) returns only jet location

        Returns
        -------
        jet_loc : int
            If debug is False, Index of jet location on latitude axis
        jet_loc, jet_loc_all, dtheta, theta_fit, lat, y_s, y_e  : tuple
            If debug is True, return lots of stuff
            TODO: document this better!!

        """
        # Find derivative of dynamical tropopause
        dtheta, theta_fit = self._poly_deriv(lat, theta_xpv)

        jet_loc_all = extrema(dtheta)[0].astype(int)
        select = self.select_jet(jet_loc_all, ushear)
        if np.max(np.abs(theta_fit[0])) == 0.0:
            # This means there was a TypeError in _poly_deriv so probably
            # none of the theta_xpv data is valid for this time/lon, so
            # set the output latitude to be 0, so it can be masked out
            out_lat = 0.0
        else:
            out_lat = lat[select]

        if debug:
            output = out_lat, jet_loc_all, dtheta, theta_fit, lat
        else:
            output = out_lat

        return output

    def select_jet(self, locs, ushear):
        """
        Select correct jet latitude given list of possible jet locations.

        Parameters
        ----------
        locs : list
            List of indicies of jet locations
        ushear : array_like
            1D array along latitude axis of maximum surface - troposphere u-wind shear

        Returns
        -------
        jet_loc : int
            Index of the jet location. Between [`0`, `lat.shape[0] - 1`]

        Notes
        -----
        * If the list of locations is empty, return ``0`` as the location, this is
          interpreted by :py:meth:`~find_jet` as missing.

        * If the list of locations is exactly one return that location.

        * Otherwise use the location with maximum zonal wind shear between lowest
          provided level and the dynamical tropopause.

        """
        if len(locs) == 0:
            # A jet has not been identified at this time/location, set the position
            # to zero so it can be masked out when the zonal median is performed
            jet_loc = 0

        elif len(locs) == 1:
            # This essentially converts a list of length 1 to a single int
            jet_loc = locs[0]

        elif len(locs) > 1:
            # The jet location, if multiple peaks are identified, should be the one
            # with maximum wind shear between the jet level and the surface
            ushear_max = np.argmax(ushear[locs])
            jet_loc = locs[ushear_max]

        return jet_loc


class STJDavisBirner(STJMetric):
    """
    Subtropical jet position metric using the method of Davis and Birner 2016.

    Parameters
    ----------
    props : :py:meth:`~STJ_PV.run_stj.JetFindRun`
        Class containing properties about the current search for the STJ
    data : :py:meth:`~STJ_PV.input_data.InputData`
        Input data class containing a year (or more) of required data

    Notes
    -----
    Based on "Climate Model Biases in the Width of the Tropical Belt Parameters",
    Davis and Birner (2016)

    The logic for the method is to subtract the surface wind and then find the
    max in the upper level wind.

    """

    # https://github.com/TropD/pytropd/blob/master/pytropd/metrics.py

    def __init__(self, props, data):
        """Initialise Metric using Davis and Birner 2016 method."""
        name = 'DavisBirner'
        super(STJDavisBirner, self).__init__(name=name, props=props, data=data)

        # Some config options should be properties for ease of access
        self.upper_p_level = self.props['upper_p_level']
        self.lower_p_level = self.props['lower_p_level']
        self.surf_p_level = self.props['surface_p_level']

        if self.data[self.data.cfg['lev']].units in ['mb', 'millibars', 'hPa']:
            self.lower_p_level /= 100.0
            self.upper_p_level /= 100.0
            self.surf_p_level /= 100.0

    def find_jet(self, shemis=True):
        """
        Find the subtropical jet using method from Davis and Birner (2016).

        doi:10.1175/JCLI-D-15-0336.1

        Parameters
        ----------
        shemis : logical, optional
            If True, find jet position in Southern Hemisphere, if False, find N.H. jet

        """
        _, hlats, hem_s = self.set_hemis(shemis)
        cfg = self.data.cfg

        if self.data[cfg['lev']][0] > self.data[cfg['lev']][-1]:
            subset = {cfg['lev']: slice(self.lower_p_level, self.upper_p_level)}
        else:
            subset = {cfg['lev']: slice(self.upper_p_level, self.lower_p_level)}

        subset.update({cfg['lat']: slice(*hlats)})
        uwnd_p = self.data.uwnd.sel(**subset)
        if uwnd_p[cfg['lat']].shape[0] == 0:
            # Reverse order of lat selection if none are found
            subset[cfg['lat']] = slice(*hlats[::-1])
            uwnd_p = self.data.uwnd.sel(**subset)

        # Subtract the surface wind from the wind in 400-100 layer
        uwnd_p = uwnd_p - self.data.uwnd.sel(**{cfg['lev']: self.surf_p_level})
        dims = uwnd_p.shape

        self.log.info('COMPUTING JET POSITION FOR %d TIMES HEMIS: %s', dims[0], hem_s)
        if self.props['zonal_opt'].lower() == 'mean':
            uzonal = uwnd_p.mean(dim=cfg['lon'])
        else:
            uzonal = uwnd_p

        jet_info = xr.apply_ufunc(
            self.find_max_wind_surface,
            uzonal,
            uzonal[cfg['lat']],
            input_core_dims=[[cfg['lev'], cfg['lat']], [cfg['lat']]],
            vectorize=True,
            dask='allowed',
            output_core_dims=[[], []],
            output_dtypes=[float, float],
        )
        # Put the parameters into place for this hemisphere
        self.out_data['lat_{}'.format(hem_s)] = jet_info[0]
        self.out_data['intens_{}'.format(hem_s)] = jet_info[1]

    def find_max_wind_surface(self, uzonal, lat, test_plot=False):
        """
        Find most equatorward maximum wind on column maximum wind surface.

        Parameters
        ----------
        uzonal : array_like
            Zonal mean zonal wind
        shemis : bool
            Flag to indicate hemisphere, true for SH

        """
        max_wind_surface = np.max(uzonal, axis=0)
        # for the given maximum wind surface, find local
        # maxima and then keep most equatorward.
        turning_points = argrelextrema(max_wind_surface, np.greater_equal)[0]
        turning_lats = lat[turning_points]

        # Take the argmin of the absolute value of the latitudes, use that index
        # This finds the most equatorward latitude, regardless of hemisphere
        lat_idx = turning_points[np.abs(turning_lats).argmin()]

        if lat_idx not in [0, 1, lat.shape[0] - 1, lat.shape[0]]:
            # If the selected index is away from the boundaries, interpolate using
            # a quadratic to find the "real" maximum location
            nearby = slice(lat_idx - 1, lat_idx + 2)
            # Cast lat and max_wind_surface as arrays of float32
            # because linalg can't handle float16
            pfit = np.polyfit(lat[nearby], max_wind_surface[nearby], deg=2)

            # Take first derivative of the quadratic, solve for maximum
            # to get the jet latitude
            stj_lat = -pfit[1] / (2 * pfit[0])

            # Evaluate 2nd degree polynomial at the maximum to get intensity
            stj_intens = np.polyval(pfit, stj_lat)
        else:
            stj_lat = lat[lat_idx]
            stj_intens = max_wind_surface[lat_idx]

        # test_plot = False
        if test_plot:
            self.test_plot(lat, max_wind_surface, lat_idx, stj_lat, stj_intens)

        return stj_lat, stj_intens

    def test_plot(self, lat, max_wind_surface, lat_idx, stj_lat, stj_intens):
        """Make a test plot for this method."""
        print("jet intensity is: ", stj_intens)
        import matplotlib.pyplot as plt

        plot_line = False

        if max(lat) < 0:
            hem = 'SH'
            plt.subplot(2, 1, 1)
            if stj_lat < -30.0:
                plot_line = True
        else:
            hem = 'NH'
            plt.subplot(2, 1, 2)
            if stj_lat > 30.0:
                plot_line = True

        if plot_line:
            plt.plot(lat, max_wind_surface)
            plt.plot(stj_lat, max_wind_surface[lat_idx], 'x')

        plt.title(hem)
        filename = 'test_davis.png'
        plt.savefig(filename)


class STJMaxWind(STJMetric):
    """
    Subtropical jet position metric: maximum zonal mean zonal wind on a pressure level.

    Parameters
    ----------
    props : :py:meth:`~STJ_PV.run_stj.JetFindRun`
        Class containing properties about the current search for the STJ
    data : :py:meth:`~STJ_PV.input_data.InputData`
        Input data class containing a year (or more) of required data

    """

    def __init__(self, props, data):
        """Initialise Metric using PV Gradient Method."""
        name = 'UMax'
        super(STJMaxWind, self).__init__(name=name, props=props, data=data)

        # Some config options should be properties for ease of access
        self.pres_lev = self.props['pres_level']
        if self.data[self.data.cfg['lev']].units in ['mb', 'hPa', 'millibars']:
            self.pres_lev /= 100.0
        self.min_lat = self.props['min_lat']

    def find_jet(self, shemis=True):
        """
        Find the subtropical jet using input parameters.

        Parameters
        ----------
        shemis : logical, optional
            If True, find jet position in Southern Hemisphere, if False, find N.H. jet

        """
        _, hlats, hem_s = self.set_hemis(shemis)
        vlat = self.data.cfg['lat']

        self.log.info(
            'COMPUTING JET POSITION FOR %d TIMES HEMIS: %s',
            self.data.uwnd.shape[0],
            hem_s,
        )
        # Setup a dict to get the desired pressure level and hemisphere
        _latlev_select = {self.data.cfg['lev']: self.pres_lev, vlat: slice(*hlats)}
        # Select the latitudes and level
        uwnd_hem = self.data.uwnd.sel(**_latlev_select)

        if uwnd_hem[vlat].shape[0] == 0:
            # This probably means that lat is decreasing with increasing index
            # so swap the hemisphere latitudes around and re-try the selection
            _latlev_select[vlat] = slice(*hlats[::-1])
            uwnd_hem = self.data.uwnd.sel(**_latlev_select)

        # Find the maximum zonal mean zonal wind at the level set in config
        uwnd_max = uwnd_hem.argmax(dim=vlat)

        # Use those indicies to find the latitude and intensity of the max wind
        # Use the latitude coordinate from the hemisphere restricted uwnd so that the
        # isel works properly (otherwise it's off by the nuber of gridpoints excluded)
        jet_lat = uwnd_hem[vlat].isel(**{vlat: uwnd_max.load()})
        jet_intens = uwnd_hem.isel(**{vlat: uwnd_max.load()})

        # Put the parameters into place for this hemisphere, taking the zonal mean first
        self.out_data['lat_{}'.format(hem_s)] = jet_lat.mean(dim=self.data.cfg['lon'])
        self.out_data['intens_{}'.format(hem_s)] = jet_intens.mean(
            dim=self.data.cfg['lon']
        )


class STJKangPolvani(STJMetric):
    """
    Subtropical jet position metric: Kang and Polvani 2010.

    Parameters
    ----------
    props : :py:meth:`~STJ_PV.run_stj.JetFindRun`
        Class containing properties about the current search for the STJ
    data : :py:meth:`~STJ_PV.input_data.InputData`
        Input data class containing a year (or more) of required data

    """

    def __init__(self, props, data):
        """Initialise Metric using Kang and Polvani Method."""
        name = 'KangPolvani'
        super(STJKangPolvani, self).__init__(name=name, props=props, data=data)
        self.wh_200 = 20000.0 / self.data.cfg["pfac"]
        self.wh_1000 = 100000.0 / self.data.cfg["pfac"]

    def find_jet(self, shemis=True):
        """
        Find the subtropical jet using input parameters.

        Parameters
        ----------
        shemis : logical, optional
            If True, find jet position in Southern Hemisphere, if False, find N.H. jet

        """
        _, hlats, hem_s = self.set_hemis(shemis)

        del_f = self.get_flux_div(hlats)
        self.get_jet_lat(del_f, hem_s)

    def get_flux_div(self, lats):
        """Calculate the meridional eddy momentum flux divergence."""
        _select = {self.data.cfg["lev"]: self.wh_200}
        _select.update(self.hemis)
        uwnd = self.data["uwnd"].sel(**_select)
        vwnd = self.data["vwnd"].sel(**_select)
        lat = uwnd[self.data.cfg["lat"]].values

        k_e = Kinetic_Eddy_Energies(uwnd, vwnd, self.data.cfg)
        k_e.get_components(zonal=True, time=False)
        k_e.calc_momentum_flux()
        del_f = k_e.del_f

        return del_f

    def get_jet_lat(self, del_f, hem_s):
        """Find the 200hpa zero crossing of meridional eddy momentum flux divergence."""
        uwnd = self.data["uwnd"].sel(**self.hemis).mean(dim=self.data.cfg["lon"])
        _vlat = self.data.cfg["lat"]
        lat = uwnd[_vlat]

        # signchange = ((np.roll(np.sign(del_f), 1) - np.sign(del_f)) != 0).values
        _delsign = del_f.pipe(np.sign)
        signchange = (_delsign.roll({_vlat: 1}, roll_coords=False) - _delsign) != 0


        stj_lat = np.zeros(uwnd.shape[0])
        stj_int = np.zeros(uwnd.shape[0])
        _uwnd200 = uwnd.sel(**{self.data.cfg["lev"]: self.wh_200})
        _uwnd1000 = uwnd.sel(**{self.data.cfg["lev"]: self.wh_1000})

        # Don't consider endpoints as having a sign change
        _no_endpoints = {_vlat: slice(1, -1)}
        signchange = signchange.isel(**_no_endpoints)
        # And restrict the wind in the same way
        _uwnd200 = _uwnd200.isel(**_no_endpoints)
        _uwnd1000 = _uwnd1000.isel(**_no_endpoints)

        stj_lat = xr.apply_ufunc(
            self.find_single_jet,
            _uwnd200,
            _uwnd1000,
            _uwnd200[_vlat],
            signchange,
            input_core_dims=[[_vlat], [_vlat], [_vlat], [_vlat]],
            vectorize=True,
            dask='allowed',
        )

        # Output the monthly mean of daily S for comparing the method
        jet_data = xr.DataArray(
            stj_lat,
            coords=[self.data["uwnd"][self.data.cfg["time"]]],
            dims=[self.data.cfg["time"]],
        )
        jet_intens = _uwnd200.sel(**{_vlat: jet_data})

        _resample = {self.data.cfg["time"]: "MS"}
        jet_data_mm = jet_data.resample(**_resample).mean()
        jet_intens_mm = jet_intens.resample(**_resample).mean()

        self.out_data["lat_{}".format(hem_s)] = jet_data_mm
        self.out_data["intens_{}".format(hem_s)] = jet_intens_mm

    def find_single_jet(self, u_high, u_low, lat, sign_change):
        """
        Get streamfunction inflection point with the largest zonal-mean zonal wind shear.

        """
        shear = u_high[sign_change] - u_low[sign_change]
        return lat[sign_change][shear.argmax()]

    def get_jet_loc(self, data, expected_lat, lat):
        """Get jet location based on sign changes of Del(f)."""
        signchange = ((np.roll(np.sign(data), 1) - np.sign(data)) != 0).values
        idx = (np.abs(lat[signchange] - expected_lat)).argmin()

        return lat[signchange][idx]

    def loop_jet_lat(self, data, expected_lat, lat):
        """Get jet location at multiple times."""
        return np.array(
            [
                self.get_jet_loc(data[tidx, :], expected_lat[tidx], lat)
                for tidx in range(data.shape[0])
            ]
        )


def lowest_valid(col):
    """
    Given 1-D array find lowest (along axis) valid data.

    Parameters
    ----------
    col : array_like
        1D array of data

    Returns
    -------
    valid : col.dtype
        Lowest (in index) valid value in `col`

    """
    return col[np.isfinite(col).argmax()]


def get_season(month):
    """Map month index to index of season [DJF -> 0, MAM -> 1, JJA -> 2, SON -> 3]."""
    seasons = np.array([0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 0])
    return seasons[month]
