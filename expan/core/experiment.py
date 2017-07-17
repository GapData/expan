import logging
import re
import warnings

import numpy as np
import pandas as pd

import expan.core.binning as binmodule
import expan.core.early_stopping as es
import expan.core.statistics as statx

from expan.core.util import getColumnNamesByType
# from expan.core.experimentdata import ExperimentData
# from expan.core.results import Results, delta_to_dataframe_all_variants, feature_check_to_dataframe, \
#     early_stopping_to_dataframe

# from expan.core.jsonable import Jsonable

# raise the same warning multiple times
warnings.simplefilter('always', UserWarning)

logger = logging.getLogger(__name__)

# TODO: add filtering functionality: we should be able to operate on this
# class to exclude data points, and save all these operations in a log that then
# is preserved in all results.
class Experiment(object):
    """
    Class which adds the analysis functions to experimental data.
    """
    def __init__(self, controlVariantName, data, metadata, reportKpiNames=None, derivedKpis=[]):
        reportKpiNames = reportKpiNames or getColumnNamesByType(data, np.float64)

        derivedKpiNames    = [k['name']    for k in derivedKpis]
        derivedKpiFormulas = [k['formula'] for k in derivedKpis]

        experimentColumnNames = ['entity', 'variant']

        # what columns do we expect to find in the data frame?
        requiredColumnNames = (set(reportKpiNames) | set(experimentColumnNames)) - set(derivedKpiNames)
        kpiNamePattern = '([a-zA-Z][0-9a-zA-Z_]*)'
        # add names from all formulas
        for formula in derivedKpiFormulas:
            names = re.findall(kpiNamePattern, formula)
            requiredColumnNames = requiredColumnNames | set(names)

        for c in requiredColumnNames:
            if c not in data:
                raise ValueError('No column %s provided'%c)

        self.data               =     data.copy()
        self.metadata           = metadata.copy()
        self.reportKpiNames     = reportKpiNames
        self.derivedKpis        = derivedKpis
        self.variantNames       = list(set(self.data.variant))
        self.controlVariantName = controlVariantName
        self.referenceKpis      = {}

        # add derived KPIs to the data frame
        for name, formula in zip(derivedKpiNames, derivedKpiFormulas):
            self.data.loc[:, name] = eval(re.sub(kpiNamePattern, r'self.data.\1.astype(float)', formula))
            self.referenceKpis[name] = re.sub(kpiNamePattern + '/', '', formula)



    def getKPIbyNameAndVariant(self, name, variant):
        return self.data.reset_index().set_index('variant').loc[variant, name]


    def __str__(self):
        # res = super(Experiment, self).__str__()

        variants = self.variant_names

        res += '\n {:d} variants: {}'.format(len(variants),
                                             ', '.join(
                                                 [('*' + k + '*') if (k == self.metadata.get('baseline_variant', '-'))
                                                  else k for k in variants]
                                             ))
        return res



    def _getWeights(self, kpi, variant):
        if kpi not in self.referenceKpis:
            return 1.0
        referenceKpi = self.referenceKpis[kpi]
        x            = self.getKPIbyNameAndVariant(referenceKpi, variant)
        zerosAndNans = sum(x == 0) + np.isnan(x).sum()
        nonZeros     = len(x) - zerosAndNans
        return nonZeros/np.nansum(x) * x

    def delta(self, method='fixed_horizon', workerArgs={}):
        workerTable = {
                'fixed_horizon'    : statx.make_delta,
                'group_sequential' : es.make_group_sequential,
                'bayes_factor'     : es.make_bayes_factor,
                'bayes_precision'  : es.make_bayes_precision,
                }

        if not method in workerTable:
            raise NotImplementedError

        worker = workerTable[method](**workerArgs)

        result = {}
        for kpi in self.reportKpiNames:
            result[kpi] = {}
            control       = self.getKPIbyNameAndVariant(kpi, self.controlVariantName)
            controlWeight = self._getWeights(kpi, self.controlVariantName)
            for variant in self.variantNames:
                treatment       = self.getKPIbyNameAndVariant(kpi, variant)
                treatmentWeight = self._getWeights(kpi, variant)
                ds = worker(x=treatment*treatmentWeight, y=control*controlWeight)
                result[kpi][variant] = {'controlVariant'   : self.controlVariantName,
                                        'treatmentVariant' : variant,
                                        'deltaStatistics'  : ds}

        return result

##
##            !!!! HERE BE DRAGONS !!!!
##

    def feature_check(self, feature_subset=None, variant_subset=None,
                      threshold=0.05, percentiles=[2.5, 97.5], assume_normal=True,
                      min_observations=20, nruns=10000, relative=False):

        """
        Compute feature check on all features, and return dataframe with column
        telling if feature check passed.

        Args:
            feature_subset (list): Features for which to perfom delta. If set to
                None all metrics are used.
            variant_subset (list): Variants to use compare against baseline. If
                set to None all variants are used.
            threshold (float): p-value used for dismissing null hypothesis (i.e.
                no difference between features for variant and baseline).

            assume_normal (boolean): specifies whether normal distribution
                assumptions can be made
            min_observations (integer): minimum observations necessary. If
                less observations are given, then NaN is returned
            nruns (integer): number of bootstrap runs to perform if assume
                normal is set to False.

        Returns:
            pd.DataFrame containing boolean column named 'ok' stating if
                feature chek was ok for the feature and variant combination
                specified in the corresponding columns.
        """
        # TODO: this should return a results structure, like all the others?
        # - can monkey patch it with a function to just get the 'ok' column

        res = Results(None, metadata=self.metadata)

        # Check if data exists TODO: Necessary or guarantted by __init__() ?
        if self.features is None:
            warnings.warn('Empty data set entered to analysis.'
                          + 'Returning empty result set')
            return res
        # TODO: Check if subsets are valid
        # If no subsets use superset
        if feature_subset is None:
            feature_subset = self.feature_names
        if variant_subset is None:
            variant_subset = self.variant_names

        deltaWorker = statx.make_delta(assume_normal, percentiles, min_observations,
                                       nruns, relative)
        # Iterate over the features
        for feature in feature_subset:
            df = (self._feature_check_all_variants(self.features.reset_index()[['entity', 'variant', feature]],
                                                   self.baseline_variant, deltaWorker))
            if res.df is None:
                res.df = df
            else:
                res.df = res.df.append(df)

        return res


    def sga(self, feature_subset=None, kpi_subset=None, variant_subset=None,
            n_bins=4, binning=None,
            assume_normal=True, percentiles=[2.5, 97.5],
            min_observations=20, nruns=10000, relative=False,
            **kwargs):
        """
        Compute subgroup delta (with confidence bounds) on all applicable
        metrics, and returns in the standard Results format.

        Does this for all non-baseline variants.

        Args:
            feature_subset (list): Features which are binned for which to
                perfom delta computations. If set to None all features are used.
            kpi_subset (list): KPIs for which to perfom delta computations.
                If set to None all features are used.
            variant_subset (list): Variants to use compare against baseline. If
                set to None all variants are used.
            n_bins (integer): number of bins to create if binning is None

            binning (list of bins): preset (if None then binning is created)
            assume_normal (boolean): specifies whether normal distribution
                assumptions can be made
            percentiles (list): list of percentile values to compute
            min_observations (integer): minimum observations necessary. If
                less observations are given, then NaN is returned
            nruns (integer): number of bootstrap runs to perform if assume
                normal is set to False.
            relative (boolean): If relative==True, then the values will be
                returned as distances below and above the mean, respectively,
                rather than the	absolute values. In	this case, the interval is
                mean-ret_val[0] to mean+ret_val[1]. This is more useful in many
                situations because it corresponds with the sem() and std()
                functions.

        Returns:
            Results object containing the computed deltas.
        """
        res = Results(None, metadata=self.metadata)

        # Check if data exists
        if self.metrics is None:
            warnings.warn('Empty data set entered to analysis.'
                          + 'Returning empty result set')
            return res
        # TODO: Check if subsets are valid
        # If no subsets use superset
        if kpi_subset is None:
            kpi_subset = self.kpi_names
        if feature_subset is None:
            feature_subset = self.feature_names
        if variant_subset is None:
            variant_subset = self.variant_names
        # Remove baseline from variant_set
        variant_subset = variant_subset - set([self.baseline_variant])
        # Iterate over the kpis, features and variants
        # TODO: Check if this is the right approach,
        # groupby and unstack as an alternative?
        deltaWorker = statx.make_delta(assume_normal, percentiles, min_observations,
                                       nruns, relative)
        for kpi in kpi_subset:
            for feature in feature_subset:
                res.df = pd.concat([
                    res.df,
                    self._subgroup_deltas(
                        self.metrics.reset_index()[['variant', feature, kpi]],
                        variants=['dummy', self.baseline_variant],
                        n_bins=n_bins,
                        deltaWorker=deltaWorker).df])
        # Return the result object
        return res


    def trend(self, kpi_subset=None, variant_subset=None, time_step=1,
              cumulative=True, assume_normal=True, percentiles=[2.5, 97.5],
              min_observations=20, nruns=10000, relative=False, **kwargs):
        """
        Compute time delta (with confidence bounds) on all applicable
        metrics, and returns in the standard Results format.

        Does this for all non-baseline variants.

        Args:
            kpi_subset (list): KPIs for which to perfom delta computations.
                If set to None all features are used.
            variant_subset (list): Variants to use compare against baseline. If
                set to None all variants are used.
            time_step (integer): time increment over which to aggregate data.
            cumulative (boolean): Trend is calculated using data from
                start till the current bin or the current bin only

            assume_normal (boolean): specifies whether normal distribution
                assumptions can be made
            percentiles (list): list of percentile values to compute
            min_observations (integer): minimum observations necessary. If
                less observations are given, then NaN is returned
            nruns (integer): number of bootstrap runs to perform if assume
                normal is set to False.
            relative (boolean): If relative==True, then the values will be
                returned as distances below and above the mean, respectively,
                rather than the	absolute values. In	this case, the interval is
                mean-ret_val[0] to mean+ret_val[1]. This is more useful in many
                situations because it corresponds with the sem() and std()
                functions.

        Returns:
            Results object containing the computed deltas.
        """
        res = Results(None, metadata=self.metadata)
        # Check if data exists
        if self.kpis_time is None:
            warnings.warn('Empty data set entered to analysis. '
                          + 'Returning empty result set')
            res.metadata['warnings']['Experiment.trend'] = \
                UserWarning('Empty data set entered to analysis.')
            return res
        # Check if time is in dataframe column
        if 'time_since_treatment' not in self.kpis_time.index.names:
            warnings.warn('Need time column for trend analysis.'
                          + 'Returning empty result set')
            res.metadata['warnings']['Experiment.trend'] = \
                UserWarning('Need time column for trend analysis.')
            return res
        # TODO: Check if subsets are valid
        # If no subsets use superset
        if kpi_subset is None:
            kpi_subset = self.kpi_names
        if variant_subset is None:
            variant_subset = self.variant_names
        # Remove baseline from variant_set
        variant_subset = variant_subset - set([self.baseline_variant])
        # Iterate over the kpis and variants
        # TODO: Check if this is the right approach
        deltaWorker = statx.make_delta(assume_normal, percentiles, min_observations,
                                       nruns, relative)
        for kpi in kpi_subset:
            for variant in variant_subset:
                # TODO: Add metadata to res.metadata
                res_obj = self._time_dependent_deltas(
                    self.kpis_time.reset_index()[['variant',
                                                  'time_since_treatment', kpi]],
                    variants=[variant, self.baseline_variant],
                    time_step=time_step,
                    cumulative=cumulative,
                    deltaWorker=deltaWorker)
                res.df = pd.concat([res.df, res_obj.df])

        # NB: assuming all binning objects based on the same feature are the same
        res.set_binning(res_obj.binning)
        # Return the result object
        return res


    def _feature_check_all_variants(self, metric_df, baseline_variant, deltaWorker):
        """Applies delta to all variants, given a metric."""
        baseline_metric = metric_df.iloc[:, 2][metric_df.variant == baseline_variant]

        def do_delta_numerical(df):
            mu, ci, ss_x, ss_y, mean_x, mean_y = deltaWorker(x=df.iloc[:, 2],
                                                             y=baseline_metric)
            return feature_check_to_dataframe(metric=df.columns[2],
                                              samplesize_variant=ss_x,
                                              mu=mu,
                                              pctiles=ci,
                                              mu_variant=mean_x)

        def do_delta_categorical(df):
            pval = statx.chi_square(x=df.iloc[:, 2], y=baseline_metric)[0]
            ss_x = statx.sample_size(df.iloc[:, 2])
            return feature_check_to_dataframe(metric=df.columns[2],
                                              samplesize_variant=ss_x,
                                              pval=pval)

        # numerical feature
        if np.issubdtype(metric_df.iloc[:, 2].dtype, np.number):
            return metric_df.groupby('variant').apply(do_delta_numerical).unstack(0)
        # categorical feature
        else:
            return metric_df.groupby('variant').apply(do_delta_categorical).unstack(0)


    def _time_dependent_deltas(self, df, variants, time_step=1, cumulative=False,
                               deltaWorker=statx.make_delta()):
        """
        Calculates the time dependent delta.

        Args:
          df (pandas DataFrame): 3 columns. The order of the columns is expected
              to be variant, time, kpi.
          variants (list of 2): 2 entries, first entry is the treatment variant,
              second entry specifies the baseline variant
          time_step (integer): time_step used for analysis.
          cumulative (Boolean): whether to accumulate values over time
          deltaWorker: a closure generated by statitics.make_delta(), holding
              the numerical parameters of delta calculations

        Returns:
          pandas.DataFrame: bin-name, mean, percentile and corresponding values
          list: binning used
        """
        # TODO: allow times to have time stamp format
        # TODO: allow start time and end time format
        # TODO: fill with zeros

        # Create time binning with time_step
        # time_bin = (lambda x: round(x / float(time_step) + 0.5) * time_step)

        # Apply time binning vectorized to each element in the input array
        # df['_tmp_time_'] = df.iloc[:, 1].apply(time_bin)

        # Get appropriate bin number
        # n_bins = len(pd.unique(df['_tmp_time_']))

        # create binning manually, ASSUMING uniform sampling
        tpoints = np.unique(df.iloc[:, 1])
        binning = binmodule.NumericalBinning(uppers=tpoints, lowers=tpoints,
                                             up_closed=[True] * len(tpoints), lo_closed=[True] * len(tpoints))

        # Push computation to _binned_deltas() function
        result = self._binned_deltas(df=df, variants=variants, binning=binning,
                                     cumulative=cumulative, label_format_str='{mid}',
                                     deltaWorker=deltaWorker)

        # Reformating of the index names in the result data frame object
        result.df.index.set_names('time', level=2, inplace=True)

        # Returning Result object containing result and the binning
        return result


    def _subgroup_deltas(self, df, variants, n_bins=4, deltaWorker=statx.make_delta()):
        """
        Calculates the feature dependent delta.
    
        Args:
          df (pandas DataFrame): 3 columns. The order of the columns is expected
              to be variant, feature, kpi.
          variants (list of 2): 2 entries, first entry is the treatment variant,
              second entry specifies the baseline variant
          n_bins (integer): number of bins to create if binning is None
          deltaWorker: a closure generated by statitics.make_delta(), holding
              the numerical parameters of delta calculations
    
    
        Returns:
          pandas.DataFrame: bin-name, mean, percentile and corresponding values
          list: binning used
        """

        # Push computation to _binned_deltas() function
        result = self._binned_deltas(df=df, variants=variants, n_bins=n_bins, binning=None,
                                     cumulative=False, label_format_str='{standard}',
                                     deltaWorker=deltaWorker)

        # TODO: Add binning to result metadata

        # Reformating of the index names in the result data frame object
        result.df.reset_index('subgroup', drop=True, inplace=True)
        result.df.index.set_names('subgroup', level=2, inplace=True)
        result.df.index.set_levels(levels=[df.columns[1]],
                                   level='subgroup_metric', inplace=True)

        # Returning Result object containing result and the binning
        return result


    def _binned_deltas(self, df, variants, n_bins=4, binning=None, cumulative=False,
                       label_format_str='{standard}', deltaWorker=statx.make_delta()):
        """
        Calculates the feature dependent delta. Only used internally. All
        calculation by subgroup_delta() and time_dependant_delta() is pushed here.
    
        Args:
          df (pandas DataFrame): 3 columns. The order of the columns is expected
              to be variant, feature, kpi.
          variants (list of 2): 2 entries, first entry is the treatment variant,
              second entry specifies the baseline variant
              TODO: currently only the baseline variant is extracted from this list
              and deltas are calculated for all variants (see bug OCTO-869)
          n_bins (integer): number of bins to create if binning is None
          binning (list of bins): preset (if None then binning is created)
          cumulative (Bool): whether to accumulate data (for time trend analysis)
          label_format_str (string): format string for the binning label function
          deltaWorker: a closure generated by statitics.make_delta(), holding
              the numerical parameters of delta calculations
    
        Returns:
          pandas.DataFrame: bin-name, mean, percentile and corresponding values
          list: binning used
        """

        # Performing binning of feature on feat2
        if binning is None:
            binning = binmodule.create_binning(df.iloc[:, 1], nbins=n_bins)

        if cumulative == True and type(binning) != binmodule.NumericalBinning:
            raise ValueError("Cannot calculate cumulative deltas for non-numerical binnings")

        # Applying binning to feat1 and feat2 arrays
        df.loc[:, '_tmp_bin_'] = binning.label(data=df.iloc[:, 1],
                                               format_str=label_format_str)

        # Initialize result object as data frame with bin keys as index
        def do_delta(f, bin_name):
            # find the corresponding bin in the baseline variant
            baseline_metric = f.iloc[:, 2][(f.iloc[:, 0] == variants[1])]
            out_df = pd.DataFrame()

            for v in f['variant'].unique():
                v_metric = f.iloc[:, 2][(f.iloc[:, 0] == v)]
                df = delta_to_dataframe_all_variants(f.columns[2], *deltaWorker(x=v_metric,
                                                                                y=baseline_metric))
                # add new index levels for variant and binning
                df['_tmp_bin_'] = bin_name
                df['variant'] = v
                df.set_index(['variant', '_tmp_bin_'], append=True, inplace=True)
                df = df.reorder_levels(['variant', '_tmp_bin_', 'metric',
                                        'subgroup_metric', 'subgroup',
                                        'statistic', 'pctile'])

                out_df = out_df.append(df)
            return out_df

        # Actual calculation
        result = pd.DataFrame()
        unique_tmp_bins = df['_tmp_bin_'].unique()
        for bin in unique_tmp_bins:
            if not cumulative:
                result = result.append(do_delta(df[df['_tmp_bin_'] == bin], bin))
            else:
                result = result.append(do_delta(df[df['_tmp_bin_'] <= bin], bin))

        # unstack variant
        result = result.unstack(0)
        # drop _tmp_bin_ in the input data frame
        del df['_tmp_bin_']

        result.index = result.index.swaplevel(0, 2)
        result.index = result.index.swaplevel(0, 1)
        # Return result and binning
        return Results(result, {'binning': binning})
