#!/usr/bin/env python

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import pymc3 as pm
import os
import argparse
import logging
from astropy.table import Table, hstack
from astropy.cosmology import Planck15 as cosmo
from sklearn.decomposition import PCA
from .util import read_snana, light_curve_event_data, filter_colors, meta_columns
from .fit_model import setup_model, produce_lc, sample_posterior

logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S', level=logging.INFO)


def load_trace(file, trace_path='.', version='2'):
    """
    Read the stored PyMC3 traces into a 3-D array with shape (nfilters, nsteps, nparams).

    Parameters
    ----------
    file : str
        Filename of the original SNANA data file.
    trace_path : str, optional
        Directory where the PyMC3 trace data is stored. Default: current directory.
    version : str, optional
        Version of the fit to use, where "version" is the character in the filename before the filter. Default: '2'.

    Returns
    -------
    lst : numpy.array
        PyMC3 trace stored as 3-D array with shape (nfilters, nsteps, nparams).
    """
    basename = os.path.basename(file)
    tracefile = os.path.join(trace_path, basename.replace('.snana.dat', '_{}{}'))
    lst = []
    t = light_curve_event_data(file)
    max_flux = t['FLUXCAL'].max()
    missing_filters = []
    for fltr in 'griz':
        tracefile_filter = tracefile.format(version, fltr)
        if os.path.exists(tracefile_filter):
            obs = t[t['FLT'] == fltr]
            model, varnames = setup_model(obs, max_flux)
            trace = pm.load_trace(tracefile_filter, model)
            trace_values = np.transpose([trace.get_values(var) for var in varnames])
            lst.append(trace_values)
        else:
            logging.warning(f"No such file or directory: '{tracefile_filter}'")
            missing_filters.append(fltr)
    if len(missing_filters) == 4:
        raise FileNotFoundError(f"No traces found for {basename}")
    for fltr in missing_filters:
        lst.insert('griz'.index(fltr), np.mean(lst, axis=0))
    lst = np.array(lst)
    return lst


def flux_to_luminosity(row):
    """
    Return the flux-to-luminosity conversion factor for the transient in a given row of a data table.

    Parameters
    ----------
    row : astropy.table.row.Row
        Astropy table row for a given transient, containing columns 'A_V' and 'redshift'.

    Returns
    -------
    flux2lum : numpy.ndarray
        Array of flux-to-luminosity conversion factors for the filters g, r, i, and z.
    """
    A_coeffs = row['A_V'] * np.array([1.16269427, 0.87191851, 0.66551667, 0.42906714])  # g, r, i, z
    dist = cosmo.luminosity_distance(row['redshift']).to('dapc').value
    flux2lum = 10. ** (A_coeffs / 2.5) * dist ** 2. * (1. + row['redshift'])
    return flux2lum


def get_principal_components(light_curves, light_curves_fit=None, n_components=6, whiten=True, reconstruct=False):
    """
    Run a principal component analysis on a list of light curves and return a list of their principal components.

    Parameters
    ----------
    light_curves : array-like
        A list of evenly-sampled model light curves.
    light_curves_fit : array-like, optional
        A list of model light curves to be used for fitting the PCA. Default: fit and transform the same light curves.
    n_components : int, optional
        The number of principal components to calculate. Default: 6.
    whiten : bool, optional
        Whiten the input data before calculating the principal components. Default: True.
    reconstruct : bool, optional
        Plot and save the reconstructed light curves to pca_reconstruction.pdf (slow). Default: False.

    Returns
    -------
    principal_components : array-like
        A list of the principal components for each of the input light curves.
    """
    if light_curves_fit is None:
        light_curves_fit = light_curves

    pcas = []
    reconstructed = []
    coefficients = []

    for lc_filter, lc_filter_fit in zip(light_curves, light_curves_fit):
        pca = PCA(n_components, whiten=whiten)
        pcas.append(pca)

        coeffs_fit = pca.fit_transform(lc_filter_fit)
        reconst = pca.inverse_transform(coeffs_fit)
        reconstructed.append(reconst)

        coeffs = pca.transform(lc_filter)
        coefficients.append(coeffs)

    coefficients = np.array(coefficients)
    reconstructed = np.array(reconstructed)
    plot_principal_components(pcas)
    if reconstruct:
        plot_pca_reconstruction(light_curves, reconstructed, coefficients)

    return coefficients, reconstructed


def plot_parameters(train_data):
    """
    Plot histograms of the model parameters stored in train_data['params']. The plot will be saved to parameters.pdf.

    Parameters
    ----------
    train_data : astropy.table.Table
        Data table containing the columns 'type' and 'params' for each supernova. Must have been grouped by 'type'.
    """
    fig, axarr = plt.subplots(4, 6, figsize=(11, 8.5), sharex='col')
    for sntype, group in zip(train_data.groups.keys['type'], train_data.groups):
        for i in range(4):
            for j in range(6):
                feature = group['params'][:, i, j]
                histrange = np.percentile(feature, (5, 95))
                axarr[i, j].hist(feature, label=sntype, range=histrange, density=True, histtype='step')
                axarr[i, j].set_yticks([])
    axarr[0, 3].legend(loc='lower center', bbox_to_anchor=(0., 1.), ncol=5)
    varnames = ['Amplitude (mag)', 'Plateau Slope', 'Plateau Duration', 'Start Time', 'Rise Time', 'Fall Time']
    for i, var in enumerate(varnames):
        axarr[-1, i].set_xlabel(var)
    for i, filt in enumerate('griz'):
        axarr[i, 0].set_ylabel(filt, rotation=0)
    axarr[-1, 0].invert_xaxis()
    fig.subplots_adjust(left=0.03, right=0.99, bottom=0.08, top=0.95, wspace=0., hspace=0.)
    fig.savefig('parameters.pdf')
    plt.close(fig)


def plot_principal_components(pcas):
    """
    Plot the principal components being used to extract features from the model light curves. The plot will be saved to
    principal_components.pdf.

    Parameters
    ----------
    pcas : list
        List of the PCA objects for each filter, after fitting.
    """
    nrows = int(pcas[0].n_components ** 0.5)
    ncols = int(np.ceil(pcas[0].n_components / nrows))
    fig, axes = plt.subplots(nrows, ncols, sharex=True)
    for pca, fltr in zip(pcas, 'griz'):
        for pc, ax in zip(pca.components_, axes.flatten()):
            ax.plot(pc, color=filter_colors[fltr], label=fltr)
    axes[0, 0].legend()
    fig.tight_layout()
    fig.savefig('principal_components.pdf')


def plot_features(train_data, classids_to_autoscale=None):
    """
    Plot histograms of the features to be used for classification. The plot will be saved to features{classids}.pdf,
    where classids is a concatenation of the integers passed in `classids_to_autoscale`.

    Parameters
    ----------
    train_data : astropy.table.Table
        Data table containing the columns 'type' and 'params' for each supernova. Must have been grouped by 'type'.
    classids_to_autoscale : set
        Set of classification IDs (integers corresponding to the supernova types) that the histogram axes should be
        autoscaled to. Some classes have a much wider dynamic range of features than others, making it difficult to plot
        all histograms on the same axes.
    """
    if classids_to_autoscale is None:
        classids_to_autoscale = set(range(len(train_data.groups)))
    ncols = int(np.ceil(train_data['features'].shape[1] / 4))
    fig, axarr = plt.subplots(4, ncols, figsize=(11, 8.5), sharex='col')
    for i in classids_to_autoscale:
        group = train_data.groups[i]
        for ax, feature in zip(axarr.flatten(), group['features'].T):
            histrange = np.percentile(feature, (5, 95))
            ax.hist(feature, label=group['type'][0], range=histrange, density=True, histtype='step', color='C'+str(i))
            ax.set_yticks([])
    for i in set(range(len(train_data.groups))) - set(classids_to_autoscale):
        group = train_data.groups[i]
        for ax, feature in zip(axarr.flatten(), group['features'].T):
            ax.autoscale(False)
            histrange = np.percentile(feature, (5, 95))
            ax.hist(feature, label=group['type'][0], range=histrange, density=True, histtype='step', color='C'+str(i))
    axarr[0, 3].legend(loc='lower center', bbox_to_anchor=(0.5, 1.), ncol=5)
    axarr[-1, 0].set_xlabel('Peak Magnitude')
    axarr[-1, 0].invert_xaxis()
    for i in range(1, axarr.shape[1]):
        axarr[-1, i].set_xlabel('PC' + str(i))
    for i, filt in enumerate('griz'):
        axarr[i, 0].set_ylabel(filt, rotation=0)
        axarr[i, 0].set_yticks([])
    fig.subplots_adjust(left=0.03, right=0.99, bottom=0.08, top=0.95, wspace=0.)
    fig.savefig('features{}.pdf'.format(''.join(str(i) for i in classids_to_autoscale)))
    plt.close(fig)


def plot_pca_reconstruction(models, reconstructed, coefficients=None):
    """
    Plot comparisons between the model light curves and the light curves reconstructed from the PCA for each transient.
    These are saved as a multi-page PDF called pdf_reconstruction.pdf.

    Parameters
    ----------
    models : array-like
        A 3-D array of model light curves with shape (nfilters, ntransients, ntimes)
    reconstructed : array-like
        A 3-D array of reconstructed light curves with shape (nfilters, ntransients, ntimes)
    coefficients : array-like, optional
        A 3-D array of the principal component coefficients with shape (nfilters, ntransients, ncomponents). If given,
        the coefficients will be printed at the top right of each plot.
    """
    with PdfPages('pca_reconstruction.pdf') as pdf:
        ax = plt.axes()
        for i in range(models.shape[1]):
            for j, fltr in enumerate('griz'):
                c = filter_colors[fltr]
                ax.plot(models[j, i], color=c)
                ax.plot(reconstructed[j, i], ls=':', color=c)
            if coefficients is not None:
                with np.printoptions(precision=2):
                    ax.text(0.99, 0.99, str(coefficients[:, i]), va='top', ha='right', transform=ax.transAxes)
            pdf.savefig()
            ax.clear()


def extract_features(t, stored_models, ndraws=10, zero_point=27.5, use_pca=True, reconstruct=False):
    """
    Extract features for a table of model light curves: the peak absolute magnitudes and principal components of the
    light curves in each filter.

    Parameters
    ----------
    t : astropy.table.Table
        Table containing the 'filename' and 'redshift' of each transient to be classified.
    stored_models : str
        If a directory, look in this directory for PyMC3 trace data and sample the posterior to produce model LCs.
        If a Numpy file, read the parameters from this file.
    ndraws : int, optional
        Number of random draws from the MCMC posterior. Default: 10. Ignored if models are read fron Numpy file.
    zero_point : float, optional
        Zero point to be used for calculating the peak absolute magnitudes. Default: 27.5 mag.
    use_pca : bool, optional
        Use the peak absolute magnitudes and principal components of the light curve as the features (default).
        Otherwise, use the model parameters directly.
    reconstruct : bool, optional
        Plot and save the reconstructed light curves to pca_reconstruction.pdf (slow). Default: False.

    Returns
    -------
    t_good : astropy.table.Table
        Slice of the input table with a 'features' column added. Rows with any bad features are excluded.
    """
    if os.path.isdir(stored_models):
        stored = {}
    else:
        stored = np.load(stored_models)
        ndraws = stored.get('ndraws', ndraws)

    if 'params' in stored:
        params = stored['params']
        logging.info(f'parameters read from {stored_models}')
    else:
        params = []
        bad_rows = []
        for i, filename in enumerate(t['filename']):
            try:
                trace = load_trace(filename, trace_path=stored_models)
                logging.info(f'loaded trace from {filename}')
            except FileNotFoundError as e:
                bad_rows.append(i)
                logging.error(e)
                continue
            if ndraws:
                params.append(sample_posterior(trace, ndraws))
            else:  # ndraws == 0 means take the average
                params.append(trace.mean(axis=1)[:, np.newaxis])
                ndraws = 1
        params = np.hstack(params)
        t.remove_rows(bad_rows)  # excluding rows that have not been fit
        np.savez_compressed('params.npz', params=params, ndraws=ndraws)
        logging.info(f'posteriors sampled from {stored_models}, saved to data_table.txt & params.npz')

    flux2lum = np.concatenate([np.tile(flux_to_luminosity(row), (ndraws, 1)) for row in t]).T
    params[:, :, 0] *= flux2lum
    params_mag = params.copy()
    params_mag[:, :, 0] = zero_point - 2.5 * np.log10(params_mag[:, :, 0])  # convert amplitude to magnitude
    if use_pca:
        time = np.linspace(0., 300., 1000)
        models = produce_lc(time, params, align_to_t0=True)
        t_good, good_models = select_good_events(t, models)
        peakmags = zero_point - 2.5 * np.log10(good_models.max(axis=2))
        logging.info('peak magnitudes extracted')
        coefficients, reconstructed = get_principal_components(good_models, good_models[:, ~t_good['type'].mask],
                                                               reconstruct=reconstruct)
        logging.info('PCA finished')
        features = np.dstack([peakmags, coefficients])
    else:
        t_good, features = select_good_events(t, params_mag[:, :, [0, 1, 2, 4, 5]])  # remove start time from features
    t_good['params'] = np.moveaxis(params_mag, 1, 0)
    t_good['features'] = np.hstack(features)

    train_data = t_good[~t_good['type'].mask].group_by('type')
    plot_parameters(train_data)
    if use_pca:
        plot_features(train_data, {0, 2})
        plot_features(train_data, {1, 3, 4})
    return t_good


def select_good_events(t, data):
    """
    Select only events with finite data for all draws. Returns the table and data for only these events.

    Parameters
    ----------
    t : astropy.table.Table, length=nevents
        Original data table with one row for each event.
    data : array-like, shape=(nfilt, nevents * ndraws, ...)
        Numpy array containing the data upon which finiteness will be judged.

    Returns
    -------
    t_good : astropy.table.Table
        Data table with n rows for each good event, where n is determined by the shape of `data`.
    good_data : array-like
        Numpy array containing only the data for good events.
    """
    good = np.isfinite(data).all(axis=(0, 2))
    ndraws = data.shape[1] // len(t)
    i_good, = np.where(good.reshape(-1, ndraws).all(axis=1))
    t_good = t[np.repeat(i_good, ndraws)]
    t_good.meta['ndraws'] = ndraws
    good_data = data[:, good]
    return t_good, good_data


def compile_data_table(filename):
    t_input = Table.read(filename, format='ascii')
    required_cols = ['id', 'A_V', 'redshift']
    missing_cols = [col for col in required_cols if col not in t_input.colnames]
    if missing_cols:
        t_meta = Table(names=required_cols, dtype=['S9', float, float], masked=True)
        for lc_file in t_input['filename']:
            t = read_snana(lc_file)
            t_meta.add_row([t.meta['SNID'], t.meta['A_V'], t.meta['REDSHIFT']])
        t_final = hstack([t_input, t_meta[missing_cols]])
    else:
        t_final = Table(t_input, masked=True)
    t_final['redshift'].mask = t_final['redshift'] <= 0.
    t_final['A_V'].format = '%.5f'
    t_final['redshift'].format = '%.4f'
    return t_final


def save_test_data(test_table):
    test_table.sort('id')
    save_table = test_table[::test_table.meta['ndraws']]
    save_table.keep_columns(meta_columns)
    save_table.write('test_data.txt', format='ascii.fixed_width_two_line', overwrite=True)
    np.savez_compressed('test_data.npz', features=test_table['features'], ndraws=test_table.meta['ndraws'])
    logging.info('test data saved to test_data.txt and test_data.npz')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input_table', type=str, help='List of input SNANA files, or input data table')
    parser.add_argument('stored_models', help='Directory where the PyMC3 trace data is stored, '
                                              'or Numpy file containing stored model parameters/LCs')
    parser.add_argument('--ndraws', type=int, default=10, help='Number of draws from the LC posterior for training set.'
                                                               ' Set to 0 to use the mean of the LC parameters.')
    parser.add_argument('--use-params', action='store_false', dest='use_pca', help='Use model parameters as features')
    parser.add_argument('--reconstruct', action='store_true',
                        help='Plot and save the reconstructed light curves to pca_reconstruction.pdf (slow)')
    args = parser.parse_args()

    logging.info('started extract_features.py')
    data_table = compile_data_table(args.input_table)
    test_data = extract_features(data_table, args.stored_models, args.ndraws, use_pca=args.use_pca,
                                 reconstruct=args.reconstruct)
    save_test_data(test_data)
    logging.info('finished extract_features.py')
