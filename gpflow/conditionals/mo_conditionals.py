# flake8: ignore=F811
# noqa: ignore=F811

from typing import Union

import tensorflow as tf

from .. import covariances
from ..features import (InducingPoints, MixedKernelSeparateMof,
                        MixedKernelSharedMof, SeparateIndependentMof,
                        SharedIndependentMof)
from ..kernels import (Combination, Mok, SeparateIndependentMok,
                       SeparateMixedMok, SharedIndependentMok)
from ..util import create_logger, default_float, default_jitter
from .dispatch import conditional_dispatch
from .util import (base_conditional, expand_independent_outputs,
                   fully_correlated_conditional,
                   independent_interdomain_conditional, mix_latent_gp,
                   rollaxis_left)

logger = create_logger()


@conditional_dispatch  # noqa: F811
def _conditional(Xnew: tf.Tensor,
                 feature: SharedIndependentMof,
                 kernel: SharedIndependentMok,
                 f: tf.Tensor,
                 full_cov=False,
                 full_output_cov=False,
                 q_sqrt=None,
                 white=False):
    """Multioutput conditional for an independent kernel and shared inducing features.
    Same behaviour as conditional with non-multioutput kernels.
    The covariance matrices used to calculate the conditional have the following shape:
    - Kuu: [M, M]
    - Kuf: [M, N]
    - Kff: N or [N, N]

    Further reference
    -----------------
    - See `gpflow.conditionals._conditional` for a detailed explanation of
      conditional in the single-output case.
    - See the multiouput notebook for more information about the multiouput framework.
    Parameters
    ----------
    :param Xnew: data matrix, size [N, D].
    :param f: data matrix, [M, P]
    :param full_cov: return the covariance between the datapoints
    :param full_output_cov: return the covariance between the outputs.
        Note: as we are using a independent kernel these covariances will be zero.
    :param q_sqrt: matrix of standard-deviations or Cholesky matrices,
        size [M, P] or [P, M, M].
    :param white: boolean of whether to use the whitened representation
    :return:
        - mean:     [N, P]
        - variance: [N, P], [P, N, N], [N, P, P] or [N, P, N, P]
        Please see `gpflow.conditional._expand_independent_outputs` for more information
        about the shape of the variance, depending on `full_cov` and `full_output_cov`.
    """
    logger.debug("Conditional: SharedIndependentMof - SharedIndepedentMok")

    Kmm = covariances.Kuu(feature, kernel, jitter=default_jitter())  # [M, M]
    Kmn = covariances.Kuf(feature, kernel, Xnew)  # [M, N]
    Knn = kernel(Xnew, full=full_cov, full_output_cov=False)
    Knn = Knn[0, ...] if full_cov else Knn[..., 0]  # [N, N] or [N]

    fmean, fvar = base_conditional(Kmn, Kmm, Knn, f, full_cov=full_cov, q_sqrt=q_sqrt,
                                   white=white)  # [N, P],  [P, N, N] or [N, P]
    return fmean, expand_independent_outputs(fvar, full_cov, full_output_cov)


@conditional_dispatch.register(feature=SeparateIndependentMof, kernel=SeparateIndependentMok)  # noqa: F811, E501
@conditional_dispatch.register(feature=SharedIndependentMof, kernel=SeparateIndependentMok)
@conditional_dispatch.register(feature=SeparateIndependentMof, kernel=SharedIndependentMok)
def _conditional(Xnew: tf.Tensor,
                 feature,
                 kernel,
                 f,
                 full_cov=False,
                 full_output_cov=False,
                 q_sqrt=None,
                 white=False):
    """Multi-output GP with independent GP priors.
    Number of latent processes equals the number of outputs (L = P).
    The covariance matrices used to calculate the conditional have the following shape:
    - Kuu: [P, M, M]
    - Kuf: [P, M, N]
    - Kff: [P, N] or [P, N, N]

    Further reference
    -----------------
    - See `gpflow.conditionals._conditional` for a detailed explanation of
      conditional in the single-output case.
    - See the multiouput notebook for more information about the multiouput framework.
    - See above for the parameters and the return value.
    """

    logger.debug("conditional: object, SharedIndependentMof, SeparateIndependentMok, object")

    # Following are: [P, M, M]  -  [P, M, N]  -  [P, N](x N)
    Kmms = covariances.Kuu(feature, kernel, jitter=default_jitter())  # [P, M, M]
    Kmns = covariances.Kuf(feature, kernel, Xnew)  # [P, M, N]
    if isinstance(kernel, Combination):
        kernels = kernel.kernels
    else:
        kernels = [kernel.kernel] * len(feature.features)
    Knns = tf.stack([k.K(Xnew) if full_cov else k.K_diag(Xnew) for k in kernels], axis=0)
    fs = tf.transpose(f)[:, :, None]  # [P, M, 1]
    # [P, 1, M, M]  or  [P, M, 1]
    q_sqrts = tf.transpose(q_sqrt)[:, :, None] if q_sqrt.shape.ndims == 2 else q_sqrt[:, None, :, :]

    def single_gp_conditional(t):
        Kmm, Kmn, Knn, f, q_sqrt = t
        return base_conditional(Kmn, Kmm, Knn, f, full_cov=full_cov, q_sqrt=q_sqrt, white=white)

    rmu, rvar = tf.map_fn(
        single_gp_conditional,
        (Kmms, Kmns, Knns, fs, q_sqrts),
        (default_float(), default_float()))  # [P, N, 1], [P, 1, N, N] or [P, N, 1]

    fmu = rollaxis_left(rmu[..., 0], 1)  # [N, P]

    if full_cov:
        fvar = rvar[..., 0, :, :]  # [P, N, N]
    else:
        fvar = rollaxis_left(rvar[..., 0], 1)  # [N, P]

    return fmu, expand_independent_outputs(fvar, full_cov, full_output_cov)


@conditional_dispatch  # noqa: F811
def _conditional(Xnew: tf.Tensor,
                 feature: Union[SharedIndependentMof, SeparateIndependentMof],
                 kernel: SeparateMixedMok,
                 f,
                 *,
                 full_cov=False,
                 full_output_cov=False,
                 q_sqrt=None,
                 white=False):
    """Interdomain conditional with independent latents.
    In this case the number of latent GPs (L) will be different than the number of outputs (P)
    The covariance matrices used to calculate the conditional have the following shape:
    - Kuu: [L, M, M]
    - Kuf: [M, L, N, P]
    - Kff: [N, P, N, P], [N, P, P], [N, P]

    Further reference
    -----------------
    - See `gpflow.conditionals._conditional` for a detailed explanation of
      conditional in the single-output case.
    - See the multiouput notebook for more information about the multiouput framework.
    - See above for the parameters and the return value.
    """

    logger.debug("Conditional: (SharedIndependentMof, SeparateIndepedentMof) - SeparateMixedMok")
    Kmm = covariances.Kuu(feature, kernel, jitter=default_jitter())  # [L, M, M]
    Kmn = covariances.Kuf(feature, kernel, Xnew)  # [M, L, N, P]
    Knn = kernel(Xnew, full=full_cov,
                 full_output_cov=full_output_cov)  # [N, P](x N)x P  or  [N, P](x P)

    return independent_interdomain_conditional(Kmn,
                                               Kmm,
                                               Knn,
                                               f,
                                               full_cov=full_cov,
                                               full_output_cov=full_output_cov,
                                               q_sqrt=q_sqrt,
                                               white=white)


@conditional_dispatch  # noqa: F811
def _conditional(Xnew: tf.Tensor,
                 feature: InducingPoints,
                 kernel: Mok,
                 f,
                 full_cov=False,
                 full_output_cov=False,
                 q_sqrt=None,
                 white=False):
    """Multi-output GP with fully correlated inducing variables.
    The inducing variables are shaped in the same way as evaluations of K, to allow a default
    inducing point scheme for multi-output kernels.
    The covariance matrices used to calculate the conditional have the following shape:
    - Kuu: [M, L, M, L]
    - Kuf: [M, L, N, P]
    - Kff: [N, P, N, P], [N, P, P], [N, P]

    Further reference
    -----------------
    - See `gpflow.conditionals._conditional` for a detailed explanation of
      conditional in the single-output case.
    - See the multiouput notebook for more information about the multiouput framework.

    Parameters
    ----------
    :param f: variational mean, [L, 1]
    :param q_sqrt: standard-deviations or cholesky, [L, 1]  or  [1, L, L]
    """

    logger.debug("Conditional: InducingPoints -- Mok")

    Kmm = covariances.Kuu(feature, kernel, jitter=default_jitter())  # [M, L, M, L]
    Kmn = covariances.Kuf(feature, kernel, Xnew)  # [M, L, N, P]
    Knn = kernel(Xnew, full=full_cov,
                 full_output_cov=full_output_cov)  # [N, P](x N)x P  or  [N, P](x P)

    M, L, N, K = [Kmn.shape[i] for i in range(Kmn.shape.ndims)]
    Kmm = tf.reshape(Kmm, (M * L, M * L))

    if full_cov == full_output_cov:
        Kmn = tf.reshape(Kmn, (M * L, N * K))
        Knn = tf.reshape(Knn, (N * K, N * K)) if full_cov else tf.reshape(Knn, (N * K, ))
        fmean, fvar = base_conditional(Kmn,
                                       Kmm,
                                       Knn,
                                       f,
                                       full_cov=full_cov,
                                       q_sqrt=q_sqrt,
                                       white=white)  # [K, 1], [1, K](x NK)
        fmean = tf.reshape(fmean, (N, K))
        fvar = tf.reshape(fvar, (N, K, N, K) if full_cov else (N, K))
    else:
        Kmn = tf.reshape(Kmn, (M * L, N, K))
        fmean, fvar = fully_correlated_conditional(Kmn,
                                                   Kmm,
                                                   Knn,
                                                   f,
                                                   full_cov=full_cov,
                                                   full_output_cov=full_output_cov,
                                                   q_sqrt=q_sqrt,
                                                   white=white)
    return fmean, fvar


@conditional_dispatch  # noqa: F811
def _conditional(Xnew: tf.Tensor,
                 feature: Union[MixedKernelSharedMof, MixedKernelSeparateMof],
                 kernel: SeparateMixedMok,
                 f,
                 *,
                 full_cov=False,
                 full_output_cov=False,
                 q_sqrt=None,
                 white=False):
    """Most efficient routine to project L independent latent gps through a mixing matrix W.
    The mixing matrix is a member of the `SeparateMixedMok` and has shape [P, L].
    The covariance matrices used to calculate the conditional have the following shape:
    - Kuu: [L, M, M]
    - Kuf: [L, M, N]
    - Kff: [L, N] or [L, N, N]

    Further reference
    -----------------
    - See `gpflow.conditionals._conditional` for a detailed explanation of
      conditional in the single-output case.
    - See the multiouput notebook for more information about the multiouput framework.
    """

    logger.debug("conditional: (MixedKernelSharedMof, MixedKernelSeparateMof), SeparateMixedMok")
    cb = conditional_dispatch.registered_function(SeparateIndependentMof, SeparateIndependentMok)
    gmu, gvar = cb(Xnew,
                   feature,
                   kernel,
                   f,
                   full_cov=full_cov,
                   q_sqrt=q_sqrt,
                   full_output_cov=False,
                   white=white)  # [N, L], [L, N, N] or [N, L]
    return mix_latent_gp(kernel.W, gmu, gvar, full_cov, full_output_cov)
