"""STJ Metric: Calculate the position of the subtropical jet in both hemispheres."""
import pdb
import numpy as np
# from numpy.polynomial import chebyshev as cby
import numpy.polynomial as poly
from scipy import interpolate
from scipy import signal as sig

import input_data
import calc_ipv as cpv


def interp_nd(lat, theta, data, lat_hr, theta_hr):
    """
    Perform interpolation on 2-dimensions on up to 4-dimensional numpy array.

    Parameters
    ----------
    lat : array_like
        One dimensional latitude coordinate array, matches a dimension of `data`
    theta : array_like
        One dimensional theta (vertical) coordinate array, matches a dimension of `data`
    data : array_like
        Data to be interpolated to high-resolution grid
    lat_hr : array_like
        1-D latitude coordinate array that `data` is interpolated to
    theta_hr : array_like
        1-D vertical coordinate array that `data` is interpolated to

    Returns
    -------
    data_interp : array_like
        Interpolated data where lat/theta dimensions are interpolated to `lat_hr` and
        `theta_hr`
    """
    lat_dim = np.where(np.array(data.shape) == lat.shape[0])[0]
    theta_dim = np.where(np.array(data.shape) == theta.shape[0])[0]

    if data.ndim == 2:
        data_f = interpolate.interp2d(lat, theta, data, kind='cubic')
        data_interp = data_f(lat_hr, theta_hr)

    elif data.ndim == 3:
        out_shape = list(data.shape)
        out_shape[lat_dim] = lat_hr.shape[0]
        out_shape[theta_dim] = theta_hr.shape[0]

        data_interp = np.zeros(out_shape)
        cmn_axis = np.where(out_shape == np.array(data.shape))[0]

        for idx0 in range(data.shape[cmn_axis]):
            data_f = interpolate.interp2d(lat, theta, data.take(idx0, axis=cmn_axis),
                                          kind='cubic')
            slc = [slice(None)] * data_interp.ndim
            slc[cmn_axis] = idx0
            data_interp[slc] = data_f(lat_hr, theta_hr)

    elif data.ndim == 4:

        out_shape = list(data.shape)
        out_shape[lat_dim] = lat_hr.shape[0]
        out_shape[theta_dim] = theta_hr.shape[0]
        data_interp = np.zeros(out_shape)

        cmn_axis = np.where(out_shape == np.array(data.shape))[0][:]
        for idx0 in range(data.shape[cmn_axis[0]]):
            for idx1 in range(data.shape[cmn_axis[1]]):
                data_slice = data.take(idx1, axis=cmn_axis[1]).take(idx0,
                                                                    axis=cmn_axis[0])
                data_f = interpolate.interp2d(lat, theta, data_slice, kind='cubic')
                # slc says which axis to place interpolated array on, it's what changes
                # with the loops
                slc = [slice(None)] * data_interp.ndim
                slc[cmn_axis[0]] = idx0
                slc[cmn_axis[1]] = idx1
                data_interp[slc] = data_f(lat_hr, theta_hr)
    return data_interp


class STJPV(object):
    """
    Metric for Subtropical jet position using dynamic tropopause on isentropic levels.
    """
    name = 'PVGrad'

    def __init__(self, props, data):
        self.props = props
        self.data = data
        if np.max(np.abs(self.data.ipv)) < 1.0:
            self.data.ipv *= 1e6    # Put PV into units of PVU from 1e-6 PVU

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

        # Initialise latitude/theta output arrays with correct shape
        dims = self.data.ipv.shape
        if self.props['zonal_opt'] == 'mean':
            self.jet_lat = np.zeros([2, dims[0]])
            self.jet_theta = np.zeros([2, dims[0]])
        else:
            self.jet_lat = np.zeros([2, dims[0], dims[-1]])
            self.jet_theta = np.zeros([2, dims[0], dims[-1]])

    def _poly_deriv(self, data, y_s=None, y_e=None, deriv=1):
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
        poly_fit = self.pfit(self.data.lat[y_s:y_e], data[y_s:y_e], self.fit_deg)
        poly_der = self.peval(self.data.lat[y_s:y_e], self.pder(poly_fit, deriv))

        return poly_der

    def find_jet(self, shemis=True):
        """
        Using input parameters find the subtropical jet.

        Parameters
        ----------
        shemis : logical, optional
            If True, find jet position in Southern Hemisphere, if False, find N.H. jet
        """
        if shemis and self.pv_lev < 0 or not shemis and self.pv_lev > 0:
            pv_lev = self.pv_lev
        else:
            pv_lev = -self.pv_lev

        # Find axis
        lat_axis = self.data.ipv.shape.index(self.data.lat.shape[0])
        lat_axis_3d = self.data.trop_theta.shape.index(self.data.lat.shape[0])

        if self.data.ipv.shape.count(self.lat.shape[0]) > 1:
            # Print a message about which matching dimension used since this
            # could be time or lev if ntimes == nlats or nlevs == nlats
            print('ASSUMING LAT DIM IS: {} ({})'.format(lat_axis, self.data.ipv.shape))

        hem_slice = [slice(None)] * self.data.ipv.ndim
        hem_slice_3d = [slice(None)] * self.data.trop_theta.ndim

        if shemis:
            hem_slice[lat_axis] = self.data.lat < 0
            hem_slice_3d[lat_axis_3d] = self.data.lat < 0
            hidx = 0
            extrema = sig.argrelmax
        else:
            hem_slice[lat_axis] = self.data.lat > 0
            hem_slice_3d[lat_axis_3d] = self.data.lat > 0
            hidx = 1
            extrema = sig.argrelmin

        # Get theta on PV==pv_level
        theta_xpv = cpv.vinterp(self.data.th_lev, self.data.ipv[hem_slice],
                                np.array([pv_lev]))
        dims = theta_xpv.shape

        uwnd = self.data.uwnd[hem_slice]
        ttrop = self.data.trop_theta[hem_slice_3d]

        if self.props['zonal_opt'] == 'mean':
            # Zonal mean stuff
            theta_xpv = np.nanmean(theta_xpv, axis=-1)
            uwnd = np.nanmean(uwnd, axis=-1)
            ttrop = np.nanmean(ttrop, axis=-1)

        for tix in range(dims[0]):

            # Get thermal tropopause intersection with dynamical tropopause
            y_s = np.abs(self.data.trop_theta[tix, :] - theta_xpv[tix, :]).argmin()
            y_e = None

            # If latitude is in decreasing order, switch start/end
            if self.data.lat[0] > self.data.lat[-1]:
                y_s, y_e = y_e, y_s

            # Find derivative of dynamical tropopause
            dtheta = self._poly_deriv(theta_xpv[tix, :], y_s=y_s, y_e=y_e)

            jet_loc_all = extrema(dtheta)[0].astype(int)
            if y_s is not None:
                # If beginning of array is cut off rather than end, add cut-off to adjust
                jet_loc_all += y_s

            jet_loc = self.select_jet(jet_loc_all, tix, uwnd[tix, ...])

            self.jet_lat[hidx, tix] = self.data.lat[tix, jet_loc]
            self.jet_theta[hidx, tix] = theta_xpv[tix, jet_loc]

    def select_jet(self, locs, tix, uwnd):
        """Select correct jet latitude."""
        if len(locs) == 0:
            self.props.log.info("NO JET LOC {}".format(tix))
            jet_loc = 0

        elif len(locs) == 1:
            jet_loc = locs[0]

        elif len(jet_loc) > 1:
            # TODO: Decide on which (if multiple local mins) to be the location
            # Should be based on wind shear at that location, for now, lowest latitude
            jet_loc = locs[np.abs(self.data.lat[locs]).argmin()]

        return jet_loc

    def save_jet(self):
        """
        Save jet position to file.
        """
        # Create output variables
        # Write jet/theta positions to file
