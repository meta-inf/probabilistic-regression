import functools

import haiku as hk
import jax
import jax.numpy as jnp
from tensorflow_probability.substrates import jax as tfp

tfd = tfp.distributions


class FunctionalParticleOptimization:
  def __init__(self, example, n_particles, model):
    net = hk.without_apply_rng(hk.transform(lambda x: model(x)))
    self.net = jax.vmap(net.apply, (0, None))
    init = jax.vmap(net.init, (0, None))
    seed_sequence = hk.PRNGSequence(666)
    self.particles = init(jnp.asarray(seed_sequence.take(n_particles)), example)
    self.priors = init(jnp.asarray(seed_sequence.take(n_particles)), example)

  def grad_step(self, x, y):
    self._grad_step(self.particles, x, y)

  @functools.partial(jax.jit, static_argnums=0)
  def _grad_step(self, particles, x, y):
    # dy_dtheta_vjp allow us to compute the jacobian-transpose times the
    # vector of # the stein operators for each particle. See eq.4 in Wang et
    # al. (2019) https://arxiv.org/abs/1902.09754.
    predictions, dy_dtheta_vjp = jax.vjp(lambda p: self.net(p, x), particles)
    predictions = jnp.asarray(predictions).transpose((1, 2, 0))

    def log_joint(predictions):
      dist = tfd.Normal(predictions[:, 0], predictions[:, 1])
      log_likelihood = dist.log_prob(y).mean()
      log_prior = 0.0
      return log_likelihood + log_prior * 1.0 / x.shape[0]

    # The predictions are independent of the evidence (a.k.a. normalization
    # factor), so the gradients of the log-posterior equal to those of the
    # log-joint.
    log_posterior_grad = jax.vmap(jax.grad(log_joint))(predictions)
    # Batch size as leading dimension.
    log_posterior_grad = log_posterior_grad.transpose((1, 0, 2))

    def kernel(predictions):
      return rbf_kernel(predictions, jax.lax.stop_gradient(predictions))

    kxy, kernel_vjp = jax.vjp(kernel, predictions)
    # Summing along the 'particles axis'.
    # See https://jax.readthedocs.io/en/latest/notebooks/autodiff_cookbook.html
    # and eq. 8 in Liu et al. (2016) https://arxiv.org/abs/1608.04471
    dkxy_dx = kernel_vjp(jnp.ones(kxy.shape))[0]
    stein_grads = ((jnp.matmul(kxy, log_posterior_grad).transpose(1, 0, 2) +
                    dkxy_dx) / len(self.particles))
    return dy_dtheta_vjp((stein_grads[..., 0], stein_grads[..., 1]))[0]

  def _log_prior(self, x):
    predictions = [self.net(params, x) for params in self.priors]
    predictions = jnp.asarray(predictions).transpose((0, 2, 1))
    mean = predictions.mean(0)
    cov = tfp.stats.cholesky_covariance(predictions)
    return tfd.MultivariateNormalTril(mean, cov)

  def predict(self, x):
    pass


def rbf_kernel(x, y):
  n_x = x.shape[0]
  pairwise = ((x[:, None] - y[None]) ** 2).sum(-1)
  bandwidth = jnp.median(pairwise.squeeze())
  bandwidth = 0.5 * bandwidth / jnp.log(n_x + 1)
  bandwidth = jnp.maximum(jax.lax.stop_gradient(bandwidth), 1e-5)
  k_xy = jnp.exp(-pairwise / bandwidth / 2)
  # Transpose to put the batch size as the leading axis.
  return k_xy.transpose()
