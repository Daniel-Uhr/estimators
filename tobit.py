import math
import warnings
import numpy as np
import pandas as pd
from scipy.optimize import minimize
import scipy.stats
from scipy.special import log_ndtr
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, mean_absolute_error

def split_left_right_censored(x, y, cens):
    counts = cens.value_counts()
    if -1 not in counts and 1 not in counts:
        warnings.warn("No censored observations; use regression methods for uncensored data")
    xs = []
    ys = []

    for value in [-1, 0, 1]:
        if value in counts:
            split = cens == value
            y_split = np.squeeze(y[split].values)
            x_split = x[split].values
        else:
            y_split, x_split = None, None
        xs.append(x_split)
        ys.append(y_split)
    return xs, ys

def tobit_neg_log_likelihood(xs, ys, params):
    x_left, x_mid, x_right = xs
    y_left, y_mid, y_right = ys

    b = params[:-1]
    s = params[-1]

    to_cat = []

    cens = False
    if y_left is not None:
        cens = True
        left = (y_left - np.dot(x_left, b))
        to_cat.append(left)
    if y_right is not None:
        cens = True
        right = (np.dot(x_right, b) - y_right)
        to_cat.append(right)
    if cens:
        concat_stats = np.concatenate(to_cat, axis=0) / s
        log_cum_norm = scipy.stats.norm.logcdf(concat_stats)
        cens_sum = log_cum_norm.sum()
    else:
        cens_sum = 0

    if y_mid is not None:
        mid_stats = (y_mid - np.dot(x_mid, b)) / s
        mid = scipy.stats.norm.logpdf(mid_stats) - math.log(max(np.finfo('float').resolution, s))
        mid_sum = mid.sum()
    else:
        mid_sum = 0

    loglik = cens_sum + mid_sum

    return - loglik

def tobit_neg_log_likelihood_der(xs, ys, params):
    x_left, x_mid, x_right = xs
    y_left, y_mid, y_right = ys

    b = params[:-1]
    s = params[-1]

    beta_jac = np.zeros(len(b))
    sigma_jac = 0

    if y_left is not None:
        left_stats = (y_left - np.dot(x_left, b)) / s
        l_pdf = scipy.stats.norm.logpdf(left_stats)
        l_cdf = log_ndtr(left_stats)
        left_frac = np.exp(l_pdf - l_cdf)
        beta_left = np.dot(left_frac, x_left / s)
        beta_jac -= beta_left

        left_sigma = np.dot(left_frac, left_stats)
        sigma_jac -= left_sigma

    if y_right is not None:
        right_stats = (np.dot(x_right, b) - y_right) / s
        r_pdf = scipy.stats.norm.logpdf(right_stats)
        r_cdf = log_ndtr(right_stats)
        right_frac = np.exp(r_pdf - r_cdf)
        beta_right = np.dot(right_frac, x_right / s)
        beta_jac += beta_right

        right_sigma = np.dot(right_frac, right_stats)
        sigma_jac -= right_sigma

    if y_mid is not None:
        mid_stats = (y_mid - np.dot(x_mid, b)) / s
        beta_mid = np.dot(mid_stats, x_mid / s)
        beta_jac += beta_mid

        mid_sigma = (np.square(mid_stats) - 1).sum()
        sigma_jac += mid_sigma

    combo_jac = np.append(beta_jac, sigma_jac / s)

    return -combo_jac

class TobitModel:
    def __init__(self, fit_intercept=True):
        self.fit_intercept = fit_intercept
        self.ols_coef_ = None
        self.ols_intercept = None
        self.coef_ = None
        self.intercept_ = None
        self.sigma_ = None

    def fit(self, x, y, cens, verbose=False):
        """
        Fit a maximum-likelihood Tobit regression
        """
        x_copy = x.copy()
        if self.fit_intercept:
            x_copy.insert(0, 'intercept', 1.0)
        else:
            x_copy.scale(with_mean=True, with_std=False, copy=False)
        init_reg = LinearRegression(fit_intercept=False).fit(x_copy, y)
        b0 = init_reg.coef_
        y_pred = init_reg.predict(x_copy)
        resid = y - y_pred
        resid_var = np.var(resid)
        s0 = np.sqrt(resid_var)
        params0 = np.append(b0, s0)
        xs, ys = split_left_right_censored(x_copy, y, cens)

        result = minimize(lambda params: tobit_neg_log_likelihood(xs, ys, params), params0, method='BFGS',
                          jac=lambda params: tobit_neg_log_likelihood_der(xs, ys, params), options={'disp': verbose})
        if verbose:
            print(result)
        self.ols_coef_ = b0[1:]
        self.ols_intercept = b0[0]
        if self.fit_intercept:
            self.intercept_ = result.x[1]
            self.coef_ = result.x[1:-1]
        else:
            self.coef_ = result.x[:-1]
            self.intercept_ = 0
        self.sigma_ = result.x[-1]

        # Calculate the standard errors, z-values, and p-values
        hessian_inv = result.hess_inv  # Inverse of Hessian matrix
        std_errors = np.sqrt(np.diag(hessian_inv))
        
        # Separate standard errors for intercept and coefficients
        if self.fit_intercept:
            coef_std_errors = std_errors[1:-1]
            intercept_std_error = std_errors[0]
        else:
            coef_std_errors = std_errors[:-1]
        
        z_values = self.coef_ / coef_std_errors
        p_values = 2 * (1 - scipy.stats.norm.cdf(np.abs(z_values)))

        # Create a summary DataFrame
        summary_df = pd.DataFrame({
            'coef': np.append(self.intercept_, self.coef_),
            'std err': np.append(intercept_std_error, coef_std_errors),
            'z': np.append(self.intercept_ / intercept_std_error, z_values),
            'P>|z|': np.append(2 * (1 - scipy.stats.norm.cdf(np.abs(self.intercept_ / intercept_std_error))), p_values).round(4),
            '[0.025': np.append(self.intercept_ - 1.96 * intercept_std_error, self.coef_ - 1.96 * coef_std_errors),
            '0.975]': np.append(self.intercept_ + 1.96 * intercept_std_error, self.coef_ + 1.96 * coef_std_errors)
        }, index=['const'] + list(x.columns))

        # Print the summary with a similar layout to the Heckman results
        print("  Tobit Regression Results")
        print("="*40)
        print(f"Dep. Variable:                     {y.name}")
        print(f"Model:                            Tobit")
        print(f"Method:                           Maximum Likelihood")
        print(f"Date:                             {pd.Timestamp.now().strftime('%a, %d %b %Y')}")
        print(f"Time:                             {pd.Timestamp.now().strftime('%H:%M:%S')}")
        print(f"No. Observations:                 {len(y)}")
        print(f"No. Censored Observations:        {len(y) - sum(cens == 0)}")
        print(f"No. Uncensored Observations:      {sum(cens == 0)}")
        print("="*74)
        print(summary_df.to_string())
        print("="*74)
        print(f"Sigma (scale):                    {self.sigma_:.4f}")
        print(f"Log-likelihood:                   {result.fun:.4f}")
        print(f"Number of Iterations:             {result.nit}")
        print("="*74)

        return self

    def predict(self, x):
        return self.intercept_ + np.dot(x, self.coef_)

    def score(self, x, y, scoring_function=mean_absolute_error):
        y_pred = np.dot(x, self.coef_)
        return scoring_function(y, y_pred)
