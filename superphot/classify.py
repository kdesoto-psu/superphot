import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import logging
from astropy.table import Table
from sklearn.ensemble import RandomForestClassifier
from sklearn.svm import SVC
from sklearn.neural_network import MLPClassifier
from sklearn.metrics import confusion_matrix, accuracy_score, f1_score
from sklearn.preprocessing import StandardScaler
from sklearn.utils import check_random_state
from sklearn.inspection import permutation_importance
from imblearn.over_sampling.base import BaseOverSampler
from imblearn.utils._docstring import Substitution, _random_state_docstring
from imblearn.over_sampling import SMOTE
from imblearn.pipeline import Pipeline
import pickle
from .util import meta_columns, plot_histograms, filter_colors, load_data
import itertools
from tqdm import tqdm
from argparse import ArgumentParser

logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S', level=logging.INFO)


def plot_confusion_matrix(confusion_matrix, classes, cmap='Blues', purity=False, title='',
                          xlabel='Photometric Classification', ylabel='Spectroscopic Classification', ax=None):
    """
    Plot a confusion matrix with each cell labeled by its fraction and absolute number.

    Based on tutorial: https://scikit-learn.org/stable/auto_examples/model_selection/plot_confusion_matrix.html

    Parameters
    ----------
    confusion_matrix : array-like
        The confusion matrix as a square array of integers.
    classes : list
        List of class labels for the axes of the confusion matrix.
    cmap : str, optional
        Name of a Matplotlib colormap to color the matrix.
    purity : bool, optional
        If False (default), aggregate by row (spec. class). If True, aggregate by column (phot. class).
    title : str, optional
        Text to go above the plot. Default: no title.
    xlabel, ylabel : str, optional
        Labels for the x- and y-axes. Default: "Spectroscopic Classification" and "Photometric Classification".
    ax : matplotlib.pyplot.axes, optional
        Axis on which to plot the confusion matrix. Default: new axis.
    """
    n_per_true_class = confusion_matrix.sum(axis=1)
    n_per_pred_class = confusion_matrix.sum(axis=0)
    if purity:
        cm = confusion_matrix / n_per_pred_class[np.newaxis, :]
    else:
        cm = confusion_matrix / n_per_true_class[:, np.newaxis]
    if ax is None:
        ax = plt.axes()
    ax.imshow(cm, interpolation='nearest', cmap=cmap, aspect='equal')
    ax.set_title(title)
    nclasses = len(classes)
    ax.set_xticks(range(nclasses))
    ax.set_yticks(range(nclasses))
    ax.set_xticklabels(['{}\n({:.0f})'.format(label, n) for label, n in zip(classes, n_per_pred_class)])
    ax.set_yticklabels(['{}\n({:.0f})'.format(label, n) for label, n in zip(classes, n_per_true_class)])
    ax.set_xticks([], minor=True)
    ax.set_yticks([], minor=True)
    ax.set_ylim(nclasses - 0.5, -0.5)

    thresh = np.nanmax(cm) / 2.
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        ax.text(j, i, f'{cm[i, j]:.2f}\n({confusion_matrix[i, j]:.0f})', ha="center", va="center",
                color="white" if cm[i, j] > thresh else "black")

    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)


@Substitution(
    sampling_strategy=BaseOverSampler._sampling_strategy_docstring.replace('dict or callable', 'dict, callable or int'),
    random_state=_random_state_docstring)
class MultivariateGaussian(BaseOverSampler):
    """Class to perform over-sampling using a multivariate Gaussian (``numpy.random.multivariate_normal``).

    Parameters
    ----------
    {sampling_strategy}

        - When ``int``, it corresponds to the total number of samples in each
          class (including the real samples). Can be used to oversample even
          the majority class. If ``sampling_strategy`` is smaller than the
          existing size of a class, that class will not be oversampled and
          the classes may not be balanced.

    {random_state}
    """
    def __init__(self, sampling_strategy='all', random_state=None):
        self.random_state = random_state
        self.mean_ = dict()
        self.cov_ = dict()
        if isinstance(sampling_strategy, int):
            self.samples_per_class = sampling_strategy
            sampling_strategy = 'all'
        else:
            self.samples_per_class = None
        super().__init__(sampling_strategy=sampling_strategy)

    def _fit_resample(self, X, y):
        self.fit(X, y)

        X_resampled = X.copy()
        y_resampled = y.copy()

        for class_sample, n_samples in self.sampling_strategy_.items():
            X_class = X[y == class_sample]
            self.mean_[class_sample] = np.mean(X_class, axis=0)
            self.cov_[class_sample] = np.cov(X_class, rowvar=False)
            if self.samples_per_class is not None:
                n_samples = self.samples_per_class - X_class.shape[0]
            if n_samples <= 0:
                continue

            self.rs_ = check_random_state(self.random_state)
            X_new = self.rs_.multivariate_normal(self.mean_[class_sample], self.cov_[class_sample], n_samples)
            y_new = np.repeat(class_sample, n_samples)

            X_resampled = np.vstack((X_resampled, X_new))
            y_resampled = np.hstack((y_resampled, y_new))

        return X_resampled, y_resampled

    def more_samples(self, n_samples):
        """Draw more samples from the same distribution of an already fitted sampler."""
        if not self.mean_ or not self.cov_:
            raise Exception('Mean and covariance not set. You must first run fit_resample(X, y).')
        classes = sorted(self.sampling_strategy_.keys())
        X = np.vstack([self.rs_.multivariate_normal(self.mean_[class_sample], self.cov_[class_sample], n_samples)
                       for class_sample in classes])
        y = np.repeat(classes, n_samples)
        return X, y


def train_classifier(pipeline, train_data):
    """
    Train a classification pipeline on `test_data`.

    Parameters
    ----------
    pipeline : imblearn.pipeline.Pipeline
        The full classification pipeline, including rescaling, resampling, and classification.
    train_data : astropy.table.Table
        Astropy table containing the test data. Must have a 'features' and a 'type' column.
    """
    pipeline.fit(train_data['features'].reshape(len(train_data), -1), train_data['type'])


def classify(pipeline, test_data, aggregate=True):
    """
    Use a trained classification pipeline to classify `test_data`.

    Parameters
    ----------
    pipeline : imblearn.pipeline.Pipeline
        The full classification pipeline, including rescaling, resampling, and classification.
    test_data : astropy.table.Table
        Astropy table containing the test data. Must have a 'features' column.
    aggregate : bool, optional
        If True (default), average the probabilities for a given supernova across the multiple model light curves.

    Returns
    -------
    results : astropy.table.Table
        Astropy table containing the supernova metadata and classification probabilities for each supernova
    """
    test_data['probabilities'] = pipeline.predict_proba(test_data['features'].reshape(len(test_data), -1))
    if aggregate:
        test_data = aggregate_probabilities(test_data)
    return test_data


def mean_axis0(x, axis=0):
    """Equivalent to the numpy.mean function but with axis=0 by default."""
    return x.mean(axis=axis)


def aggregate_probabilities(table):
    """
    Average the classification probabilities for a given supernova across the multiple model light curves.

    Parameters
    ----------
    table : astropy.table.Table
        Astropy table containing the metadata for a supernova and the classification probabilities ('probabilities')

    Returns
    -------
    results : astropy.table.Table
        Astropy table containing the supernova metadata and average classification probabilities for each supernova
    """
    table = table[[col for col in table.colnames if col in meta_columns] + ['probabilities']]
    grouped = table.filled().group_by(table.colnames[:-1])
    results = grouped.groups.aggregate(mean_axis0)
    if 'type' in results.colnames:
        results['type'] = np.ma.array(results['type'])
    return results


def validate_classifier(pipeline, train_data, test_data=None, aggregate=True):
    """
    Validate the performance of a machine-learning classifier using leave-one-out cross-validation.

    Parameters
    ----------
    pipeline : imblearn.pipeline.Pipeline
        The full classification pipeline, including rescaling, resampling, and classification.
    train_data : astropy.table.Table
        Astropy table containing the training data. Must have a 'features' column and a 'type' column.
    test_data : astropy.table.Table, optional
        Astropy table containing the test data. Must have a 'features' column to which to apply the trained classifier.
        If None, use the training data itself for validation.
    aggregate : bool, optional
        If True (default), average the probabilities for a given supernova across the multiple model light curves.

    Returns
    -------
    results : astropy.table.Table
        Astropy table containing the supernova metadata and classification probabilities for each supernova
    """
    if test_data is None:
        test_data = train_data
    train_classifier(pipeline, train_data)
    test_data['probabilities'] = pipeline.predict_proba(test_data['features'].reshape(len(test_data), -1))
    for filename in tqdm(train_data['filename'], desc='Cross-validation'):
        train_index = train_data['filename'] != filename
        test_index = test_data['filename'] == filename
        train_classifier(train_data[train_index])
        test_features = test_data['features'][test_index].reshape(np.count_nonzero(test_index), -1)
        test_data['probabilities'][test_index] = pipeline.predict_proba(test_features)
    if aggregate:
        test_data = aggregate_probabilities(test_data)
    return test_data


def make_confusion_matrix(results, classes=None, p_min=0., saveto=None, purity=False, binary=False):
    """
    Given a data table with classification probabilities, calculate and plot the confusion matrix.

    Parameters
    ----------
    results : astropy.table.Table
        Astropy table containing the supernova metadata and classification probabilities (column name = 'probabilities')
    classes : array-like, optional
        Labels corresponding to the 'probabilities' column. If None, use the sorted entries in the 'type' column.
    p_min : float, optional
        Minimum confidence to be included in the confusion matrix. Default: include all samples.
    saveto : str, optional
        Save the plot to this filename. If None, the plot is displayed and not saved.
    purity : bool, optional
        If False (default), aggregate by row (true label). If True, aggregate by column (predicted label).
    binary : bool, optional
        If True, plot a SNIa vs non-SNIa (CCSN) binary confusion matrix.
    """
    results = results[~results['type'].mask]
    if classes is None:
        classes = np.unique(results['type'])
    if binary:
        results['type'] = ['SNIa' if sntype == 'SNIa' else 'CCSN' for sntype in results['type']]
        SNIa_probs = results['probabilities'][:, np.where(classes == 'SNIa')[0][0]]
        classes = np.array(['CCSN', 'SNIa'])
        predicted_types = np.choose(np.round(SNIa_probs).astype(int), classes)
        include = (SNIa_probs > p_min) | (SNIa_probs < 1. - p_min)
    else:
        predicted_types = classes[np.argmax(results['probabilities'], axis=1)]
        include = results['probabilities'].max(axis=1) > p_min
    cnf_matrix = confusion_matrix(results['type'][include], predicted_types[include])
    title = 'Purity' if purity else 'Completeness'
    size = (len(classes) + 1.) * 5. / 6.
    if size > 3.:  # only add stats to title if figure is big enough
        accuracy = accuracy_score(results['type'][include], predicted_types[include])
        f1 = f1_score(results['type'][include], predicted_types[include], average='macro')
        title += f' ($N={include.sum():d}$, $A={accuracy:.2f}$, $F_1={f1:.2f}$)'
        xlabel = 'Photometric Classification'
        ylabel = 'Spectroscopic Classification'
    else:
        xlabel = 'Phot. Class.'
        ylabel = 'Spec. Class.'
    fig = plt.figure(figsize=(size, size))
    plot_confusion_matrix(cnf_matrix, classes, purity=purity, title=title, xlabel=xlabel, ylabel=ylabel)
    fig.tight_layout()
    if saveto is None:
        plt.show()
    else:
        fig.savefig(saveto)


def load_results(filename):
    results = Table.read(filename, format='ascii')
    if 'type' in results.colnames:
        results['type'] = np.ma.array(results['type'])
    classes = [col for col in results.colnames if col not in meta_columns]
    results['probabilities'] = np.stack([results[sntype].data for sntype in classes]).T
    results.remove_columns(classes)
    return results


def write_results(test_data, classes, filename):
    """
    Write the classification results to a text file.

    Parameters
    ----------
    test_data : astropy.table.Table
        Astropy table containing the supernova metadata and the classification probabilities ('probabilities').
    classes : list
        The labels that correspond to the columns in 'probabilities'
    filename : str
        Name of the output file
    """
    output = test_data[[col for col in test_data.colnames if col in meta_columns]]
    output['MWEBV'].format = '%.4f'
    output['redshift'].format = '%.4f'
    for i, classname in enumerate(classes):
        output[classname] = test_data['probabilities'][:, i]
        output[classname].format = '%.3f'
    output.write(filename, format='ascii.fixed_width_two_line', overwrite=True)
    logging.info(f'classification results saved to {filename}')


def plot_feature_importance(pipeline, train_data, width=0.8, nsamples=1000, saveto=None):
    """
    Plot a bar chart of feature importance using mean decrease in impurity, with permutation importances overplotted.

    Parameters
    ----------
    pipeline : sklearn.pipeline.Pipeline or imblearn.pipeline.Pipeline
        The trained pipeline for which to plot feature importances. Steps should be named 'classifier' and 'sampler'.
    train_data : astropy.table.Table
        Data table containing 'features' and 'type' for training when calculating permutation importances. Must also
        include 'featnames' and 'filters' in `train_data.meta`.
    width : float, optional
        Total width of the bars in units of the separation between bars. Default: 0.8.
    nsamples : int, optional
        Number of samples to draw for the fake validation data set. Default: 1000.
    saveto : str, optional
        Filename to which to save the plot. Default: show instead of saving.
    """
    logging.info('calculating feature importance')
    featnames = train_data.meta['featnames']
    filters = train_data.meta['filters']

    featnames = np.append(featnames, 'Random Number')
    xoff = 0.5 * width / filters.size * np.linspace(1 - filters.size, filters.size - 1, filters.size)
    xranges = np.arange(featnames.size) + xoff[:, np.newaxis]
    random_feature_train = np.random.random(len(train_data))
    random_feature_validate = np.random.random(nsamples * pipeline.classes_.size)
    fig, ax = plt.subplots(1, 1)
    for real_features, xrange, fltr in zip(np.moveaxis(train_data['features'], 1, 0), xranges, filters):
        X = np.hstack([real_features, random_feature_train[:, np.newaxis]])
        pipeline.fit(X, train_data['type'])
        importance0 = pipeline.named_steps['classifier'].feature_importances_

        X_val, y_val = pipeline.named_steps['sampler'].more_samples(nsamples)
        X_val[:, -1] = random_feature_validate
        result = permutation_importance(pipeline.named_steps['classifier'], X_val, y_val, n_jobs=-1)
        importance = result.importances_mean
        std = result.importances_std

        c = filter_colors.get(fltr)
        ax.barh(xrange[:-1], importance0[:-1], width / filters.size, color=c)
        ax.errorbar(importance, xrange, xerr=std, fmt='o', color=c, mfc='w')

    proxy_artists = [Patch(color='gray'), ax.errorbar([], [], xerr=[], fmt='o', color='gray', mfc='w')]
    ax.legend(proxy_artists, ['Mean Decrease in Impurity', 'Permutation Importance'], loc='best')
    for i, featname in enumerate(featnames):
        ax.text(-0.03, i, featname, ha='right', va='center', transform=ax.get_yaxis_transform())
    ax.set_yticks([])
    ax.set_yticks(xranges.flatten(), minor=True)
    ax.set_yticklabels(np.repeat(filters, featnames.size), minor=True, size='x-small', ha='center')
    ax.invert_yaxis()
    ax.set_xlabel('Feature Importance')
    ax.set_xlim(0., ax.get_xlim()[1])
    fig.tight_layout()
    if saveto is None:
        plt.show()
    else:
        fig.savefig(saveto)
    plt.close(fig)


def _plot_confusion_matrix_from_file():
    parser = ArgumentParser()
    parser.add_argument('filename', type=str, help='Filename containing the table of classification results.')
    parser.add_argument('--pmin', type=float, default=0.,
                        help='Minimum confidence to be included in the confusion matrix.')
    parser.add_argument('--saveto', type=str, help='If provided, save the confusion matrix to this file.')
    parser.add_argument('--purity', action='store_true', help='Aggregate by column instead of by row.')
    parser.add_argument('--binary', action='store_true', help='Plot a SNIa vs non-SNIa (CCSN) binary confusion matrix.')
    args = parser.parse_args()

    results = load_results(args.filename)
    make_confusion_matrix(results, p_min=args.pmin, saveto=args.saveto, purity=args.purity, binary=args.binary)


def _train():
    parser = ArgumentParser()
    parser.add_argument('train_data', help='Filename of the metadata table for the training set.')
    parser.add_argument('--classifier', choices=['rf', 'svm', 'mlp'], default='rf', help='The classification algorithm '
                        'to use. Current choices are "rf" (random forest; default), "svm" (support vector machine), or '
                        '"mlp" (multilayer perceptron).')
    parser.add_argument('--sampler', choices=['mvg', 'smote'], default='mvg', help='The resampling algorithm to use. '
                        'Current choices are "mvg" (multivariate Gaussian; default) or "smote" (synthetic minority '
                        'oversampling technique).')
    parser.add_argument('--random-state', type=int, help='Seed for the random number generator (for reproducibility).')
    parser.add_argument('--output', default='pipeline.pickle',
                        help='Filename to which to save the pickled classification pipeline.')
    args = parser.parse_args()

    logging.info('started training')
    train_data = load_data(args.train_data)
    if train_data['type'].mask.any():
        raise ValueError('training data is missing values in the "type" column')

    if args.classifier == 'rf':
        clf = RandomForestClassifier(criterion='entropy', max_features=5, n_jobs=-1, random_state=args.random_state)
    elif args.classifier == 'svm':
        clf = SVC(C=1000, gamma=0.1, probability=True, random_state=args.random_state)
    elif args.classifier == 'mlp':
        clf = MLPClassifier(hidden_layer_sizes=(10, 5), alpha=1e-5, early_stopping=True, random_state=args.random_state)
    else:
        raise NotImplementedError(f'{args.classifier} is not a recognized classifier type')

    if args.sampler == 'mvg':
        sampler = MultivariateGaussian(sampling_strategy=1000, random_state=args.random_state)
    elif args.sampler == 'smote':
        sampler = SMOTE(random_state=args.random_state)
    else:
        raise NotImplementedError(f'{args.sampler} is not a recognized sampler type')

    pipeline = Pipeline([('scaler', StandardScaler()), ('sampler', sampler), ('classifier', clf)])
    train_classifier(pipeline, train_data)
    with open(args.output, 'wb') as f:
        pickle.dump(pipeline, f)
    logging.info('finished training')


def _classify():
    parser = ArgumentParser()
    parser.add_argument('pipeline', help='Filename of the pickled classification pipeline.')
    parser.add_argument('test_data', help='Filename of the metadata table for the test set.')
    parser.add_argument('--output', default='test_data', help='Filename (without extension) to save the results.')
    args = parser.parse_args()

    logging.info('started classification')
    with open(args.pipeline, 'rb') as f:
        pipeline = pickle.load(f)
    test_data = load_data(args.test_data)

    test_data = classify(pipeline, test_data, aggregate=False)
    test_data['prediction'] = pipeline.classes_[test_data['probabilities'].argmax(axis=1)]
    plot_histograms(test_data, 'params', 'prediction', var_kwd='paramnames', row_kwd='filters',
                    saveto=f'{args.output}_parameters.pdf')
    plot_histograms(test_data, 'features', 'prediction', var_kwd='featnames', row_kwd='filters',
                    saveto=f'{args.output}_features.pdf')

    results = aggregate_probabilities(test_data)
    write_results(results, pipeline.classes_, f'{args.output}_results.txt')
    logging.info('finished classification')


def _validate():
    parser = ArgumentParser()
    parser.add_argument('pipeline', help='Filename of the pickled classification pipeline.')
    parser.add_argument('validation_data', help='Filename of the metadata table for the validation set.')
    parser.add_argument('--train-data', help='Filename of the metadata table for the training set, if different than '
                                             'the validation set.')
    parser.add_argument('--pmin', type=float, default=0.,
                        help='Minimum confidence to be included in the confusion matrix.')
    args = parser.parse_args()

    logging.info('started validation')
    with open(args.pipeline, 'rb') as f:
        pipeline = pickle.load(f)

    validation_data = load_data(args.validation_data)
    if validation_data['type'].mask.any():
        raise ValueError('validation data is missing values in the "type" column')

    if args.train_data is None:
        train_data = validation_data
    else:
        train_data = load_data(args.train_data)
        if train_data['type'].mask.any():
            raise ValueError('training data is missing values in the "type" column')

    plot_feature_importance(pipeline, train_data, saveto='feature_importance.pdf')

    results_validate = validate_classifier(pipeline, train_data, validation_data)
    write_results(results_validate, pipeline.classes_, 'validation.txt')
    make_confusion_matrix(results_validate, pipeline.classes_, args.pmin, 'confusion_matrix.pdf')
    make_confusion_matrix(results_validate, pipeline.classes_, args.pmin, 'confusion_matrix_purity.pdf', purity=True)
    logging.info('finished validation')
