from time import time, sleep

from ..core import Agent
from ..utils import *

from cryptotrader.models import apriori as models
from cryptotrader.optimizers import gradient as gd
from cryptotrader.optimizers import gt
from cryptotrader.models import risk

import optunity as ot
import pandas as pd
import talib as tl
from decimal import Decimal
from datetime import timedelta
from numpy import diag, sqrt, log, trace
from numpy.linalg import inv

from ..exceptions import *

from scipy.signal import argrelextrema
from scipy.optimize import minimize
from scipy import stats
import cvxopt as opt
import cvxopt.solvers as optsolvers
import warnings
import talib as ta
optsolvers.options['show_progress'] = False


# Base class
class APrioriAgent(Agent):
    """
    Apriori abstract trading agent.
    Use this class to create trading strategies and deploy to Trading environment
    to train and deploy models directly into the market
    """
    def __init__(self, fiat, name=""):
        """

        :param fiat: str: symbol to use as quote
        :param name: str: agent name
        """
        super().__init__(name=name)
        self.epsilon = 1e-16
        self.fiat = fiat
        self.step = 0
        self.name = name
        self.log = {}

    # Model methods
    def predict(self, obs):
        """
        Select action on actual observation
        :param obs:
        :return:
        """
        raise NotImplementedError()

    def rebalance(self, obs):
        return NotImplementedError()

    # Train methods
    def set_params(self, **kwargs):
        raise NotImplementedError("You must overwrite this class in your implementation.")

    def fit(self, env, nb_steps, batch_size, search_space, constraints=None, action_repetition=1, callbacks=None, verbose=1,
            visualize=False, nb_max_start_steps=0, start_step_policy=None, log_interval=10000, start_step=0,
            nb_max_episode_steps=None, noise_abs=0.0):
        """
        Fit the model on parameters on the environment
        :param env: BacktestEnvironment instance
        :param nb_steps: Number of optimization evals
        :param batch_size: Size of the batch for each optimization pass
        :param search_space: Parameter search space
        :param constrains: Function returning False when constrains are violated
        :param action_repetition:
        :param callbacks:
        :param verbose:
        :param visualize:
        :param nb_max_start_steps:
        :param start_step_policy:
        :param log_interval:
        :param nb_max_episode_steps: Number of steps for one episode
        :param noise_abs: Noise radius to use on sample runs
        :return: tuple: Optimal parameters, information about the optimization process
        """
        try:
            # Initialize train
            env.training = True
            i = 0
            t0 = time()

            if verbose:
                print("Optimizing model for %d steps with batch size %d..." % (nb_steps, batch_size))

            ### First, optimize benchmark
            env.optimize_benchmark(nb_steps * 100, verbose=True)

            ## Now optimize model w.r.t benchmark
            # First define optimization constrains
            # Ex constrain:
            # @ot.constraints.constrained([lambda mean_type,
            #         ma1,
            #         ma2,
            #         std_span,
            #         alpha_up,
            #         alpha_down: ma1 < ma2])

            if not constraints:
                constraints = [lambda *args, **kwargs: True]

            # Initialize buffer
            optimization_rewards = []

            # Then, define optimization routine
            @ot.constraints.constrained(constraints)
            @ot.constraints.violations_defaulted(-100)
            def find_hp(**kwargs):
                try:
                    # Init variables
                    nonlocal i, nb_steps, t0, env, nb_max_episode_steps, optimization_rewards

                    # Sample params
                    self.set_params(**kwargs)

                    # Try model for a batch
                    # sample environment
                    r, rstd = self.test(env,
                                    nb_episodes=batch_size,
                                    action_repetition=action_repetition,
                                    callbacks=callbacks,
                                    visualize=visualize,
                                    nb_max_episode_steps=nb_max_episode_steps,
                                    nb_max_start_steps=nb_max_start_steps,
                                    start_step_policy=start_step_policy,
                                    start_step=start_step,
                                    noise_abs=noise_abs,
                                    verbose=False)

                    # Log batch reward
                    optimization_rewards.append(r)

                    # Increment step counter
                    i += 1

                    # Update progress
                    if verbose:
                        print("Optimization step {0}/{1}, r: {2:.8f}, r std: {3:.8f}, mean r: {4:.8f} ETC: {5}                     ".format(i,
                                                                            nb_steps,
                                                                            r,
                                                                            rstd,
                                                                            np.mean(optimization_rewards),
                                                                            str(pd.to_timedelta((time() - t0) * (nb_steps - i), unit='s'))),
                              end="\r")
                        t0 = time()

                    # Average rewards and return
                    return r

                except KeyboardInterrupt:
                    raise ot.api.fun.MaximumEvaluationsException(0)

            # Define params search space
            # Ex search space:
            #
            # hp = {
            #     'ma1': [2, env.obs_steps],
            #     'ma2': [2, env.obs_steps],
            #     'std_span': [2, env.obs_steps],
            #     'alpha_up': [1e-8, 1],
            #     'alpha_down': [1e-8, 1]
            #     }
            #
            # search_space = {'mean_type':{'simple': hp,
            #                              'exp': hp,
            #                              'kama': hp
            #                              }
            #                 }

            print("\nOptimizing model...")

            # Call optimizer
            opt_params, info, _ = ot.maximize_structured(find_hp,
                                              num_evals=nb_steps,
                                              search_space=search_space
                                              )

            # Update model params with optimal
            self.set_params(**opt_params)

            # Set flag off
            env.training = False

            # Return optimal params and information
            return opt_params, info

        except KeyboardInterrupt:

            # If interrupted, clean after yourself
            env.training = False
            print("\nOptimization interrupted by user.")
            return opt_params, info


# Test and benchmark
class TestAgent(APrioriAgent):
    """
    Test agent for debugging
    """
    def __repr__(self):
        return "Test"

    def __init__(self, obs_shape, fiat="BTC"):
        super().__init__(fiat)
        self.obs_shape = obs_shape

    def predict(self, obs):
        # Assert obs is valid
        assert obs.shape == self.obs_shape, "Wrong obs shape."

        for val in obs.applymap(lambda x: isinstance(x, Decimal) and Decimal.is_finite(x)).all():
            assert val, ("Non decimal value found in obs.", obs.applymap(lambda x: isinstance(x, Decimal) and Decimal.is_finite(x)).all())

        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)
        else:
            return self.get_portfolio_vector(obs)

    def rebalance(self, obs):
        return self.predict(obs.apply(convert_to.decimal, raw=True))

    def test(self, env, nb_episodes=1, action_repetition=1, callbacks=None, visualize=False,
             nb_max_episode_steps=None, nb_max_start_steps=0, start_step_policy=None, verbose=False):
        """
        Test agent on environment
        """
        try:
            # Get env params
            self.fiat = env._fiat

            # Reset observations
            env.reset_status()
            env.reset()

            # Get max episode length
            if nb_max_episode_steps is None:
                nb_max_episode_steps = env.data_length

            #Reset counters
            t0 = time()
            self.step = 0
            episode_reward = 1

            while True:
                try:
                    action = self.rebalance(env.get_observation(True))
                    obs, reward, _, status = env.step(action)
                    episode_reward *= np.float64(reward)

                    self.step += 1

                    if visualize:
                        env.render()

                    if verbose:
                        print(">> step {0}/{1}, {2} % done, Cumulative Reward: {3}, ETC: {4}, Samples/s: {5:.04f}                   ".format(
                            self.step,
                            nb_max_episode_steps - env.obs_steps - 2,
                            int(100 * self.step / (nb_max_episode_steps - env.obs_steps - 2)),
                            episode_reward,
                            str(pd.to_timedelta((time() - t0) * ((nb_max_episode_steps - env.obs_steps - 2)
                                                                 - self.step), unit='s')),
                            1 / (time() - t0)
                        ), end="\r", flush=True)
                        t0 = time()

                    if status['OOD'] or self.step == nb_max_episode_steps:
                        return episode_reward

                    if status['Error']:
                        # e = status['Error']
                        # print("Env error:",
                        #       type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                        break

                except Exception as e:
                    print("Model Error:",
                          type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                    raise e

        except KeyboardInterrupt:
            print("\nKeyboard Interrupt: Stoping backtest\nElapsed steps: {0}/{1}, {2} % done.".format(self.step,
                                                                             nb_max_episode_steps,
                                                                             int(100 * self.step / nb_max_episode_steps)))


class TestLookAhead(APrioriAgent):
    """
    Test for look ahead bias
    """
    def __repr__(self):
        return "TestLookAhead"

    def __init__(self, mr=False, fiat="BTC"):
        super().__init__(fiat=fiat)
        self.mr = mr

    def predict(self, obs):
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        if self.mr:
            price_relative = np.append(prices.apply(lambda x: safe_div(x[-2], x[-1])).values, [1.0])
        else:
            price_relative = np.append(prices.apply(lambda x: safe_div(x[-1], x[-2])).values, [1.0])

        return price_relative

    def rebalance(self, obs):
        factor = self.predict(obs)
        position = np.zeros_like(factor)
        position[np.argmax(factor)] = 1
        return position


class RandomWalk(APrioriAgent):
    """
    Dummytrader that sample actions from a random process
    """
    def __repr__(self):
        return "RandomWalk"

    def __init__(self, random_process=None, activation='softmax', fiat="BTC"):
        """
        Initialization method
        :param env: Apocalipse driver instance
        :param random_process: Random process used to sample actions from
        :param activation: Portifolio activation function
        """
        super().__init__(fiat)

        self.random_process = random_process
        self.activation = activation

    def predict(self, obs):
        """
        Performs a single step on the environment
        """
        if self.random_process:
            if self.activation == 'softmax':
                return array_normalize(self.random_process.sample())
            elif self.activation == 'simplex':
                return self.simplex_proj(self.random_process.sample())
            else:
                return np.array(self.random_process.sample())
        else:
            if self.activation == 'softmax':
                return array_normalize(np.random.random(obs.columns.levels[0].shape[0]))
            elif self.activation == 'simplex':
                return self.simplex_proj(np.random.random(obs.columns.levels[0].shape[0]))
            else:
                return np.random.random(obs.columns.levels[0].shape[0])

    def rebalance(self, obs):
        return self.predict(obs)


class BuyAndHold(APrioriAgent):
    """
    Equally distribute cash at the first step and hold
    """
    def __repr__(self):
        return "BuyAndHold"

    def __init__(self, fiat="BTC"):
        super().__init__(fiat)

    def predict(self, obs):
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs - 1)
            return array_normalize(action)
        else:
            return self.get_portfolio_vector(obs)[:-1]

    def rebalance(self, obs):
        position = self.predict(obs)
        position.resize(obs.columns.levels[0].shape[0])
        position[-1] = self.get_portfolio_vector(obs)[-1]
        return position


class ConstantRebalance(APrioriAgent):
    """
    Equally distribute portfolio every step
    """
    def __repr__(self):
        return "ContantRebalance"

    def __init__(self, position=None, fiat="BTC"):
        super().__init__(fiat)
        if position:
            self.position = array_normalize(position)
        else:
            self.position = False

    def predict(self, obs):
        if not isinstance(self.position, np.ndarray):
            n_symbols = obs.columns.levels[0].shape[0]
            self.position = array_normalize(np.ones(n_symbols - 1))
            self.position = np.append(self.position, [0.0])

        return self.position

    def rebalance(self, obs):
        factor = self.predict(obs)
        return factor

    def set_params(self, **kwargs):
        self.position = np.append(array_normalize(np.array([kwargs[key]
                                            for key in kwargs]))[:-1], [0.0])


# No regret
class ONS(APrioriAgent):
    """
    Online Newton Step algorithm.
    Reference:
        A.Agarwal, E.Hazan, S.Kale, R.E.Schapire.
        Algorithms for Portfolio Management based on the Newton Method, 2006.
        http://machinelearning.wustl.edu/mlpapers/paper_files/icml2006_AgarwalHKS06.pdf
        http://rob.schapire.net/papers/newton_portfolios.pdf
    """

    def __repr__(self):
        return "ONS"

    def __init__(self, delta=0.125, beta=1, eta=0., fiat="BTC", name="ONS"):
        """
        :param delta, beta, eta: Model parameters. See paper.
        """
        super().__init__(fiat=fiat, name=name)
        self.delta = delta
        self.beta = beta
        self.eta = eta

    def predict(self, obs):
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        price_relative = np.append(prices.apply(lambda x: safe_div(x[-1], x[-2])).values, [1.0])
        return price_relative

    def rebalance(self, obs):
        if not self.init:
            self.n_pairs = obs.columns.levels[0].shape[0]
            self.A = np.mat(np.eye(self.n_pairs))
            self.b = np.mat(np.zeros(self.n_pairs)).T
            self.init = True

        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-1)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)

        else:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)

    def update(self, b, x):
        # calculate gradient
        grad = np.clip(np.mat(safe_div(x, np.dot(b, x))).T, -self.clip, self.clip)
        # update A
        self.A += grad * grad.T
        # update b
        self.b += (1 + safe_div(1., self.beta)) * grad

        # projection of p induced by norm A
        pp = self.projection_in_norm(self.delta * self.A.I * self.b, self.A)

        return pp * (1 - self.eta) + np.ones(len(x)) / float(len(x)) * self.eta

    def projection_in_norm(self, x, M):
        """
        Projection of x to simplex induced by matrix M. Uses quadratic programming.
        """
        m = M.shape[0]

        # Constrains matrices
        P = opt.matrix(2 * M)
        q = opt.matrix(-2 * M * x)
        G = opt.matrix(-np.eye(m))
        h = opt.matrix(np.zeros((m, 1)))
        A = opt.matrix(np.ones((1, m)))
        b = opt.matrix(1.)

        # Solve using quadratic programming
        sol = opt.solvers.qp(P, q, G, h, A, b)
        return np.squeeze(sol['x'])

    def set_params(self, **kwargs):
        self.delta = kwargs['delta']
        self.beta = kwargs['beta']
        self.eta = kwargs['eta']
        if 'mr' in kwargs:
            self.mr = bool(kwargs['mr'])


class OGS(APrioriAgent):
    """
    Online gradient step with AdaGrad algorithm optimizer
    """

    def __repr__(self):
        return "OGS"

    def __init__(self, factor=models.price_relative, lr=1, eta=0., clip_grads=1e6, damping=0.99, mr=False, fiat="BTC", name="ONS"):
        """
        :param delta, beta, eta: Model parameters. See paper.
        """
        super().__init__(fiat=fiat, name=name)

        self.lr = lr
        self.damping = damping
        self.factor = factor
        self.eta = eta
        self.clip = clip_grads
        self.mr = mr

        self.init = False

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        return np.append(self.factor(obs).iloc[-1].values, [1.0])

    def rebalance(self, obs):
        if not self.init:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = array_normalize(action)

            # AdaGrad square gradient
            self.gti = np.ones_like(self.crp)

            self.init = True

        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-1)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)
        else:
            return self.crp

    def update(self, b, x):
        # calculate gradient
        grad = np.clip(safe_div(x, np.dot(b, x)), -self.clip, self.clip) - 1
        self.gti = np.clip(self.gti * self.damping + grad ** 2, 0.0, 1e8)
        adjusted_grad = safe_div(grad, self.gti)

        # update b, we are using relative log return benchmark, so we want to maximize here
        b += self.lr * adjusted_grad

        # projection of p
        pp = simplex_proj(b)

        return pp * (1 - self.eta) + np.ones(len(x)) / float(len(x)) * self.eta

    def set_params(self, **kwargs):
        if 'lr'in kwargs:
            self.lr = kwargs['lr']
        if 'eta' in kwargs:
            self.eta = kwargs['eta']
        if 'mr' in kwargs:
            self.mr = bool(kwargs['mr'])
        if 'damping' in kwargs:
            self.damping = kwargs['damping']


class MW(APrioriAgent):
    """
    Multiplicative-Weights algorithm
    """

    def __repr__(self):
        return "Multiplicative Weights"

    def __init__(self, factor=models.price_relative, lr=1, fiat="BTC", name="MW"):
        """
        :param delta, beta, eta: Model parameters. See paper.
        """
        super().__init__(fiat=fiat, name=name)

        self.lr = lr
        self.factor = factor

        self.init = False

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        return np.append(self.factor(obs).iloc[-1].values, [1.0])


    def rebalance(self, obs):
        if not self.init:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = array_normalize(action)

            # AdaGrad square gradient
            self.gti = np.ones_like(self.crp)

            self.init = True

        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-1)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)
        else:
            return self.crp

    def update(self, b, x):
        # update b, we are using relative log return benchmark, so we want to maximize here
        b += self.lr * b * x

        return simplex_proj(b)

    def set_params(self, **kwargs):
        if 'lr'in kwargs:
            self.lr = kwargs['lr']


class ORAMW(APrioriAgent):
    """
    Online Risk Averse Multiplicative Weights
    This is an algorithm that uses multiplicative weights with gradient experts
    and Extreme Risk Index for portfolio allocation
    References:
        Extreme Risk Index:
        https://arxiv.org/pdf/1505.04045.pdf
    """
    def __repr__(self):
        return "Online Risk Averse Multiplicative Weights"

    def __init__(self, window=120, k=0.1, lr=0.5, mpc=1, fiat="BTC", name='ORAGS'):
        super().__init__(fiat=fiat, name=name)
        self.window = window - 1
        self.k = k
        self.mpc = mpc
        self.opt = gt.MultiplicativeWeights(lr)

        # Extreme risk index
        self.cons = [
            {'type': 'eq', 'fun': lambda w: w.sum() - 1}, # Simplex region
            {'type': 'ineq', 'fun': lambda w: w}, # Positive bound
            # Maximum position concentration constraint
            {'type': 'ineq', 'fun': lambda w: self.mpc - np.linalg.norm(w[:-1], ord=np.inf)}
        ]

        self.crp = None
        self.b = None
        self.init = False

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        prices = obs.xs('open',level=1, axis=1)

        factor = np.hstack([prices.rolling(2).apply(
            lambda x: np.log(safe_div(x[-1], x[-2]))).dropna().values, np.zeros((self.window, 1))])

        factor2 = np.hstack([prices.rolling(2).apply(
            lambda x: np.log(safe_div(x[-2], x[-1]))).dropna().values, np.zeros((self.window, 1))])

        return factor, factor2

    def loss(self, w, R, Z, x):
        # minimize allocation risk
        return risk.ERI(R, Z, w) + w[-1] * np.exp(x).mean() * x.var()

    def update(self, b, x, x2):

        # Update portfolio with no regret
        last_x = x[-1, :]
        leader = np.zeros_like(last_x)
        leader[np.argmax(last_x)] = -1

        b = simplex_proj(self.opt.optimize(leader, b))

        # Manage allocation risk
        b = minimize(
            self.loss,
            b,
            args=(*risk.polar_returns(x2, self.k), last_x),
            constraints=self.cons,
            options={'maxiter': 300},
            tol=1e-6,
            bounds=tuple((0,1) for _ in range(b.shape[0]))
        )

        # Log variables
        self.log['lr'] = "%.4f" % self.opt.lr
        self.log['mpc'] = "%.4f" % self.mpc
        self.log['risk'] = "%.6f" % b['fun']

        # Return best portfolio
        return b['x']

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.init:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = array_normalize(action)
            self.b = self.crp
            # AdaGrad square gradient, started with ones for stability
            self.init = True

        if self.step:
            # b = self.get_portfolio_vector(obs)
            x, x2 = self.predict(obs)
            self.b = self.update(self.b, x, x2)
            return self.b

        else:
            return self.crp

    def set_params(self, **kwargs):
        if 'window' in kwargs:
            self.window = int(kwargs['window'])
        if 'k' in kwargs:
            self.k = kwargs['k']
        if 'lr' in kwargs:
            self.opt.lr = kwargs['lr']
        if 'mpc' in kwargs:
            self.mpc = kwargs['mpc']


class NRS(APrioriAgent):
    """
    Pursuit and Evade No-Regret System
    """
    def __repr__(self):
        return "Pursuit and Evade No-Regret System"

    def __init__(self, window=120, k=0.1, lr=0.5, gradlr=1e-2, beta=0.5,
                 mpc=1, fiat="BTC", name='NRS'):
        super().__init__(fiat=fiat, name=name)
        self.window = window - 1
        self.k = k
        self.mpc = mpc
        self.opt = gt.MultiplicativeWeights(lr)
        self.pe = gt.PursuitAndEvade(gradlr)
        self.beta = beta
        self.lr = lr

        # Extreme risk index
        self.cons = [
            {'type': 'eq', 'fun': lambda w: w.sum() - 1}, # Simplex region
            {'type': 'ineq', 'fun': lambda w: w}, # Positive bound
            {'type': 'ineq', 'fun': lambda w: self.mpc - np.linalg.norm(w, ord=np.inf)}
            # Maximum position concentration constraint
        ]

        self.b = None
        self.w = None
        self.score = None
        self.init = False

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        prices = obs.xs('open', level=1, axis=1)
        log_returns = np.hstack([prices.rolling(2).apply(
            lambda x: np.log(safe_div(x[-1], x[-2]))).dropna().values, np.zeros((self.window, 1))])
        return log_returns

    # Pareto Extreme Risk Index
    @staticmethod
    def estimate_alpha(R):
        return safe_div((R.shape[0] - 1), np.log(safe_div(R[:-1], R[-1])).sum())

    @staticmethod
    def estimate_gamma(alpha, Z, w):
        return (1 / (Z.shape[0] - 1)) * np.power(np.clip(w * Z[:-1].T, 0.0, np.inf), alpha).sum()

    def loss_tf(self, w, alpha, Z, x):
        # minimize allocation risk
        return self.estimate_gamma(alpha, Z, w) + w[-1] * np.exp(x).mean() * x.var()

    def loss_eri(self, w, alpha, Z, b):
        # minimize allocation risk
        return self.estimate_gamma(alpha, Z, w) + np.linalg.norm(b - w) ** 2

    def update(self, b, x):
        # Update portfolio with no regret
        last_x = x[-1, :]
        self.r_hat = self.beta * self.r_hat + (1 - self.beta) * last_x

        # ERI params
        R, Z = risk.polar_returns(-x, self.k)
        alpha = self.estimate_alpha(R)

        # Compute experts scores
        for i in range(self.score.shape[0]):
            self.score[i] = self.score[i] * self.beta + (1 - self.beta) * np.dot(last_x, self.w[i])

        cons = self.cons + [{'type': 'eq', 'fun': lambda w:
            np.dot(w, self.r_hat) - np.clip(0.001, 0.0, self.r_hat.max())}]

        # Choose to follow or pursuit
        best_w = self.w[np.argmax(self.score)]
        if np.allclose(b, best_w, 1e-2, 1e-2):
            action = 'follow'
        else:
            action = 'pursuit'

        # Update experts
        leader = np.zeros_like(last_x)
        leader[np.argmax(last_x)] = -1

        # self.opt1.lr = self.lr / np.exp((self.score[1] + self.score[0]))
        self.w[0] = minimize(
            self.loss_tf,
            self.opt.optimize(leader, self.w[0]),
            args=(alpha, Z, last_x),
            constraints=self.cons,
            options={'maxiter': 666},
            tol=1e-7,
            bounds=tuple((0,1) for _ in range(b.shape[0]))
        )['x']

        self.w[1] = minimize(
            self.loss_eri,
            self.w[1],
            args=(alpha, Z, self.w[1]),
            constraints=cons,
            options={'maxiter': 666},
            tol=1e-7,
            bounds=tuple((0,1) for _ in range(b.shape[0]))
        )['x']

        if action == 'follow':
            b = simplex_proj(self.w[np.argmax(self.score)])
        elif action == 'pursuit':
            b = simplex_proj(self.pe.optimize(self.w[np.argmax(self.score)], b))

        # Log variables
        self.log['score'] = "tf: %.4f, mr: %.4f" % (self.score[0], self.score[1])
        self.log['ERI'] = "%.8f" % risk.ERI(*risk.polar_returns(-x, self.k), b)
        self.log['TCVaR'] = "%.2f" % risk.TCVaR(*risk.fit_t(np.dot(x, b)))
        self.log['alpha'] = "%.2f" % alpha
        self.log['CC'] = "%.2f" % np.power(b, 2).sum() ** -1
        self.log['action'] = action
        self.log['lr'] = "%.2f" % self.opt.lr
        self.log['beta'] = "%.2f" % self.beta
        self.log['mpc'] = "%.2f" % self.mpc

        return b

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.step:
            n_pairs = obs.columns.levels[0].shape[0]
            crp = np.ones(n_pairs)
            crp[-1] = 0

            self.crp = array_normalize(crp)
            self.w = np.vstack([self.crp.reshape([1, -1]) for _ in range(2)])
            self.b = self.crp
            self.r_hat = np.zeros(n_pairs)
            self.score = np.zeros(self.w.shape[0])
            return self.crp

        x = self.predict(obs)
        self.b = self.update(self.b, x)
        return self.b


class AdaHedge(APrioriAgent):
    """
    AdaHedge
    https://arxiv.org/pdf/1301.0534.pdf
    """
    def __repr__(self):
        return "AdaHedge"

    def __init__(self, window=3, eta=0.999, fiat="BTC", name='AdaHedge'):
        super().__init__(fiat=fiat, name=name)
        self.window = window - 1
        self.opt = gt.HigherOrderMultiplicativeWeights(1, 8)
        self.eta = eta

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        prices = obs.xs('open',level=1, axis=1)
        factor = np.hstack([prices.rolling(2).apply(
            lambda x: np.log(safe_div(x[-1], x[-2]))).dropna().values, np.zeros((self.window, 1))])

        return factor

    def update(self, b, x):
        # Update portfolio with no regret
        last_x = x[-1, :]
        experts_losses = -last_x

        h = np.dot(experts_losses, b)
        m = (-1 / self.opt.lr) * np.log(np.dot(b, exp_approx(-self.opt.lr * experts_losses, order=8)))
        delta = h - m
        self.delta = self.eta * self.delta + delta

        # Update learning rate
        self.opt.lr = np.log(b.shape[0]) / self.delta

        # Update weights
        b = array_normalize(self.opt.optimize(experts_losses, b * self.eta + (1 - self.eta) * self.crp))

        # Log variables
        self.log['lr'] = "%.4f" % self.opt.lr
        self.log['delta'] = "%.4f" % self.delta
        self.log['eta'] = "%.4f" % self.eta

        # Return updated portfolio
        return b

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.step:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = self.b = array_normalize(action)

            # AdaHedge accumulators
            self.delta = 1

            return self.crp

        x = self.predict(obs)
        self.b = self.update(self.b, x)
        return self.b


class Flipflop(APrioriAgent):
    """
    Flipflop
    https://arxiv.org/pdf/1301.0534.pdf
    """

    def __repr__(self):
        return "Flipflop"

    def __init__(self, window=120, phi=1.1, alpha=0.1, fiat="BTC", name='Flipflop'):
        super().__init__(fiat=fiat, name=name)
        self.window = window - 1
        self.opt = gt.HigherOrderMultiplicativeWeights(1, order=8)
        self.scale = np.array([phi / alpha, alpha])

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        prices = obs.xs('open', level=1, axis=1)

        factor = np.hstack([prices.rolling(2).apply(
            lambda x: np.log(safe_div(x[-1], x[-2]))).dropna().values, np.zeros((self.window, 1))])

        return factor

    def update(self, b, x):
        # Update portfolio with no regret
        last_x = x[-1, :]
        experts_loss = -last_x

        # losses
        h = np.dot(experts_loss, b)
        m = (-1 / self.opt.lr) * np.log(np.dot(b, np.exp(-self.opt.lr * experts_loss)))
        delta = h - m

        # Accumulate delta
        self.delta[self.regime] += delta

        if self.delta[self.regime] > self.scale[self.regime] * self.delta[1 - self.regime]:
            self.regime = 1 - self.regime

        if self.regime == 0:
            self.opt.lr = 1e3
        else:
            self.opt.lr = np.log(b.shape[0]) / self.delta[1]

        b = simplex_proj(self.opt.optimize(experts_loss, b))

        # Log variables
        self.log['lr'] = "%.4f" % self.opt.lr
        self.log['delta'] = "%s" % str(self.delta)

        # Return best portfolio
        return b

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.step:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = self.b = array_normalize(action)

            # AdaHedge accumulators
            self.delta = np.ones(2, dtype=np.float64) * 1e-3
            self.regime = 0

            return self.crp

        x = self.predict(obs)
        self.b = self.update(self.b, x)
        return self.b


# Pattern trading
class HarmonicTrader(APrioriAgent):
    """
    Fibonacci harmonic pattern trader
    """

    def __repr__(self):
        return "HarmonicTrader"

    def __init__(self, peak_order=7, err_allowed=0.05, decay=0.99, activation=simplex_proj, fiat="BTC", name="Harmonic"):
        """
        Fibonacci trader init method
        :param peak_order: Extreme finder movement magnitude threshold
        :param err_allowed: Pattern error margin to be accepted
        :param decay: float: Decay rate for portfolio selection. Between 0 and 1
        :param fiat: Fiat symbol to use in trading
        """
        super().__init__(fiat, name=name)
        self.err_allowed = err_allowed
        self.peak_order = peak_order
        self.alpha = [1., 1.]
        self.decay = decay
        self.activation = activation

    def find_extreme(self, obs):
        max_idx = argrelextrema(obs.open.values, np.greater, order=self.peak_order)[0]
        min_idx = argrelextrema(obs.open.values, np.less, order=self.peak_order)[0]
        extreme_idx = np.concatenate([max_idx, min_idx, [obs.shape[0] - 1]])
        extreme_idx.sort()
        return obs.open.iloc[extreme_idx]

    def calc_intervals(self, extremes):
        XA = extremes.iloc[-2] - extremes.iloc[-1]
        AB = extremes.iloc[-3] - extremes.iloc[-2]
        BC = extremes.iloc[-4] - extremes.iloc[-3]
        CD = extremes.iloc[-5] - extremes.iloc[-4]

        return XA, AB, BC, CD

    def find_pattern(self, obs, c1, c2, c3):
        try:
            XA, AB, BC, CD = self.calc_intervals(self.find_extreme(obs))

            # Gartley fibonacci pattern
            AB_range = np.array([c1[0] - self.err_allowed, c1[1] + self.err_allowed]) * abs(XA)
            BC_range = np.array([c2[0] - self.err_allowed, c2[1] + self.err_allowed]) * abs(AB)
            CD_range = np.array([c3[0] - self.err_allowed, c3[1] + self.err_allowed]) * abs(BC)

            if AB_range[0] < abs(AB) < AB_range[1] and \
                                    BC_range[0] < abs(BC) < BC_range[1] and \
                                    CD_range[0] < abs(CD) < CD_range[1]:
                if XA > 0 and AB < 0 and BC > 0 and CD < 0:
                    return 1
                elif XA < 0 and AB > 0 and BC < 0 and CD > 0:
                    return -1
                else:
                    return 0
            else:
                return 0
        except IndexError:
            return 0

    def is_gartley(self, obs):
        return self.find_pattern(obs, c1=(0.618, 0.618), c2=(0.382, 0.886), c3=(1.27, 1.618))

    def is_butterfly(self, obs):
        return self.find_pattern(obs, c1=(0.786, 0.786), c2=(0.382, 0.886), c3=(1.618, 2.618))

    def is_bat(self, obs):
        return self.find_pattern(obs, c1=(0.382, 0.5), c2=(0.382, 0.886), c3=(1.618, 2.618))

    def is_crab(self, obs):
        return self.find_pattern(obs, c1=(0.382, 0.618), c2=(0.382, 0.886), c3=(2.24, 3.618))

    def predict(self, obs):
        pairs = obs.columns.levels[0]
        action = np.zeros(pairs.shape[0] - 1)
        for i, pair in enumerate(pairs):
            if pair is not self.fiat:
                pattern = np.array([pattern(obs[pair]) for pattern in [self.is_gartley,
                                                                       self.is_butterfly,
                                                                       self.is_bat,
                                                                       self.is_crab]]).sum()

                action[i] = pattern

        return action

    def rebalance(self, obs):
        if self.step:
            pairs = obs.columns.levels[0]
            prev_port = self.get_portfolio_vector(obs)
            action = self.predict(obs)
            port_vec = np.zeros(pairs.shape[0])
            for i in range(pairs.shape[0] - 1):
                if action[i] >= 0:
                    port_vec[i] = max(0.,
                                      (self.decay * prev_port[i] + (1 - self.decay)) + self.alpha[0] * action[
                                          i])
                else:
                    port_vec[i] = max(0.,
                                      (self.decay * prev_port[i] + (1 - self.decay)) + self.alpha[1] * action[
                                          i])

            port_vec[-1] = max(0, 1 - port_vec.sum())

        else:
            n_pairs = obs.columns.levels[0].shape[0]
            port_vec = np.ones(n_pairs)
            port_vec[-1] = 0

        return self.activation(port_vec)

    def set_params(self, **kwargs):
        self.err_allowed = kwargs['err_allowed']
        self.peak_order = int(kwargs['peak_order'])
        self.decay = kwargs['decay']
        self.alpha = [kwargs['alpha_up'], kwargs['alpha_down']]


# Mean reversion
class PAMR(APrioriAgent):
    """
    Passive aggressive mean reversion strategy for portfolio selection.

    Reference:
        B. Li, P. Zhao, S. C.H. Hoi, and V. Gopalkrishnan.
        Pamr: Passive aggressive mean reversion strategy for portfolio selection, 2012.
        https://link.springer.com/content/pdf/10.1007%2Fs10994-012-5281-z.pdf
    """
    def __repr__(self):
        return "PAMR"

    def __init__(self, eps=0.03, C=2444, variant="PAMR1", fiat="BTC", name="PAMR"):
        """
        :param sensitivity: float: Sensitivity parameter. Lower is more sensitive.
        :param C: float: Aggressiveness parameter. For PAMR1 and PAMR2 variants.
        :param variant: str: The variant of the proposed algorithm. It can be PAMR, PAMR1, PAMR2.
        :
        """
        super().__init__(fiat=fiat, name=name)
        self.eps = eps
        self.C = C
        self.variant = variant

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        price_relative = np.append(prices.apply(lambda x: safe_div(x[-2], x[-1])).values, [1.0])

        return price_relative

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-2)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)
        else:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x <= eps
        and minimize distance to previous portfolio.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        # x_mean = np.mean(x)
        # if np.dot(b, x) >= 1:
        #     le = max(0., np.dot(b, x) - (1 + self.sensitivity))
        # else:
        #     le = max(0, (1 - self.sensitivity) - np.dot(b, x))

        x_mean = np.mean(x)

        le = max(0., np.dot(b, x) - self.eps)

        if self.variant == 'PAMR0':
            lam = safe_div(le, np.linalg.norm(x - x_mean) ** 2)
        elif self.variant == 'PAMR1':
            lam = min(self.C, safe_div(le, np.linalg.norm(x - x_mean) ** 2))
        elif self.variant == 'PAMR2':
            lam = safe_div(le, (np.linalg.norm(x - x_mean) ** 2 + 0.5 / self.C))
        else:
            raise TypeError("Bad variant param.")

        # limit lambda to avoid numerical problems
        lam = min(100000, lam)

        # update portfolio
        b += lam * (x - x_mean)

        # project it onto simplex
        return simplex_proj(b)

    def set_params(self, **kwargs):
        self.eps = kwargs['eps']
        if 'C' in kwargs:
            self.C = kwargs['C']
        self.variant = kwargs['variant']


class OLMAR(APrioriAgent):
    """
        On-Line Portfolio Selection with Moving Average Reversion

        Reference:
            B. Li and S. C. H. Hoi.
            On-line portfolio selection with moving average reversion, 2012.
            http://icml.cc/2012/papers/168.pdf
        """

    def __repr__(self):
        return "OLMAR"

    def __init__(self, window=7, eps=0.02, fiat="BTC", name="OLMAR"):
        """
        :param window: integer: Lookback window size.
        :param eps: float: Threshold value for updating portfolio.
        """
        super().__init__(fiat=fiat, name=name)
        self.window = window
        self.eps = eps

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        price_predict = np.append(safe_div(prices.iloc[-self.window:].mean().values, prices.iloc[-1].values), [1.0])

        return price_predict

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-2)
            price_predict = self.predict(obs)
            return self.update(prev_posit, price_predict)
        else:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x >= eps
        and minimize distance to previous weights.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        xt = np.dot(b, x)
        x_mean = np.mean(x)

        lam = max(0., safe_div((xt - self.eps), np.linalg.norm(x - x_mean) ** 2))

        # limit lambda to avoid numerical problems
        lam = min(100000, lam)

        # update portfolio
        b += lam * (x - x_mean)

        # project it onto simplex
        return simplex_proj(b)

    def set_params(self, **kwargs):
        self.eps = kwargs['eps']
        self.window = int(kwargs['window'])


class CWMR(APrioriAgent):
    """ Confidence weighted mean reversion.
    Reference:
        B. Li, S. C. H. Hoi, P.L. Zhao, and V. Gopalkrishnan.
        Confidence weighted mean reversion strategy for online portfolio selection, 2013.
        http://jmlr.org/proceedings/papers/v15/li11b/li11b.pdf
    """
    def __repr__(self):
        return "CWMR"

    def __init__(self, eps=-0.5, confidence=0.95, var=0, rebalance=True, fiat="BTC", name="CWMR"):
        """
        :param eps: Mean reversion threshold (expected return on current day must be lower
                    than this threshold). Recommended value is -0.5.
        :param confidence: Confidence parameter for profitable mean reversion portfolio.
                    Recommended value is 0.95.
        """
        super(CWMR, self).__init__(fiat=fiat, name=name)

        # input check
        if not (0 <= confidence <= 1):
            raise ValueError('confidence must be from interval [0,1]')
        if rebalance:
            self.reb = -2
        else:
            self.reb = -1
        self.eps = eps
        self.theta = stats.norm.ppf(confidence)
        self.var = var

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        price_relative = prices.apply(lambda x: safe_div(x[-1], x[-2])).values
        price_relative = np.append(price_relative, [1.0])

        return price_relative

    def update(self, b, x):
        # initialize
        m = len(x)
        mu = np.matrix(b).T
        sigma = self.sigma
        theta = self.theta
        eps = self.eps
        x = np.matrix(x).T  # matrices are easier to manipulate

        # 4. Calculate the following variables
        M = mu.T * x
        V = x.T * sigma * x
        x_upper = sum(diag(sigma) * x) / trace(sigma)

        # 5. Update the portfolio distribution
        mu, sigma = self.calculate_change(x, x_upper, mu, sigma, M, V, theta, eps)

        # 6. Normalize mu and sigma
        mu = simplex_proj(mu)
        sigma = sigma / (m ** 2 * trace(sigma))
        """
        sigma(sigma < 1e-4*eye(m)) = 1e-4;
        """
        self.sigma = sigma

        return np.array(mu.T).ravel()

    def calculate_change(self, x, x_upper, mu, sigma, M, V, theta, eps):
        if not self.var:
            # lambda from equation 7
            foo = (V - x_upper * x.T * np.sum(sigma, axis=1)) / M ** 2 + V * theta ** 2 / 2.
            a = foo ** 2 - V ** 2 * theta ** 4 / 4
            b = 2 * (eps - log(M)) * foo
            c = (eps - log(M)) ** 2 - V * theta ** 2

            a, b, c = a[0, 0], b[0, 0], c[0, 0]

            lam = max(0,
                      (-b + sqrt(b ** 2 - 4 * a * c)) / (2. * a),
                      (-b - sqrt(b ** 2 - 4 * a * c)) / (2. * a))
            # bound it due to numerical problems
            lam = min(lam, 1E+7)

            # update mu and sigma
            U_sqroot = 0.5 * (-lam * theta * V + sqrt(lam ** 2 * theta ** 2 * V ** 2 + 4 * V))
            mu = mu - lam * sigma * (x - x_upper) / M
            sigma = inv(inv(sigma) + theta * lam / U_sqroot * diag(x) ** 2)
            """
            tmp_sigma = inv(inv(sigma) + theta*lam/U_sqroot*diag(xt)^2);
            % Don't update sigma if results are badly scaled.
            if all(~isnan(tmp_sigma(:)) & ~isinf(tmp_sigma(:)))
                sigma = tmp_sigma;
            end
            """

            return mu, sigma

        else:
            """ First variant of a CWMR outlined in original article. It is
            only approximation to the posted problem. """
            # lambda from equation 7
            foo = (V - x_upper * x.T * np.sum(sigma, axis=1)) / M ** 2
            a = 2 * theta * V * foo
            b = foo + 2 * theta * V * (eps - log(M))
            c = eps - log(M) - theta * V

            a, b, c = a[0, 0], b[0, 0], c[0, 0]

            lam = max(0,
                      (-b + sqrt(b ** 2 - 4 * a * c)) / (2. * a),
                      (-b - sqrt(b ** 2 - 4 * a * c)) / (2. * a))
            # bound it due to numerical problems
            lam = min(lam, 1E+7)

            # update mu and sigma
            mu = mu - lam * sigma * (x - x_upper) / M
            sigma = inv(inv(sigma) + 2 * lam * theta * diag(x) ** 2)
            """
            tmp_sigma = inv(inv(sigma) + theta*lam/U_sqroot*diag(xt)^2);
            % Don't update sigma if results are badly scaled.
            if all(~isnan(tmp_sigma(:)) & ~isinf(tmp_sigma(:)))
                sigma = tmp_sigma;
            end
            """

            return mu, sigma

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        n_pairs = obs.columns.levels[0].shape[0]
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=self.reb)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)
        else:
            action = np.ones(n_pairs)
            action[-1] = 0
            self.sigma = np.matrix(np.eye(n_pairs) / n_pairs ** 2)
            return array_normalize(action)

    def set_params(self, **kwargs):
        self.eps = kwargs['eps']
        self.theta = stats.norm.ppf(kwargs['confidence'])


class STMR(APrioriAgent):
    """
    Short term mean reversion strategy for portfolio selection.

    Original algo by José Olímpio Mendes
    27/11/2017
    """

    def __repr__(self):
        return "STMR"

    def __init__(self, eps=0.02, eta=0.0, window=120, k=0.1, mpc=1, rc=1, fiat="BTC", name="STMR"):
        """
        :param sensitivity: float: Sensitivity parameter. Lower is more sensitive.
        """
        super().__init__(fiat=fiat, name=name)
        self.eps = eps
        self.eta = eta
        self.window = window - 1
        self.k = k
        self.mpc = mpc
        self.rc = rc

        self.init = False

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        price_relative = np.append(prices.apply(lambda x: safe_div(x[-2], x[-1]) - 1).values, [0.0])

        return price_relative

    def polar_returns(self, obs):
        """
        Calculate polar return
        :param obs: pandas DataFrame
        :return: return radius, return angles
        """
        # Find relation between price and previous price
        prices = obs.xs('open', level=1, axis=1).astype(np.float64).iloc[-self.window - 1:]
        price_relative = np.hstack([np.mat(prices.rolling(2).apply(
            lambda x: safe_div(x[-2], x[-1]) - 1).dropna().values), np.zeros((self.window, 1))])

        # Find the radius and the angle decomposition on price relative vectors
        radius = np.linalg.norm(price_relative, ord=1, axis=1)
        angle = np.divide(price_relative, np.mat(radius).T)

        # Select the 'window' greater values on the observation
        index = np.argpartition(radius, -(int(self.window * self.k) + 1))[-(int(self.window * self.k) + 1):]
        index = index[np.argsort(radius[index])]

        # Return the radius and the angle for extreme found values
        return radius[index][::-1], angle[index][::-1]

    def estimate_alpha(self, radius):
        """
        Estimate pareto's distribution alpha
        :param radius: polar return radius
        :return: alpha
        """
        return safe_div((radius.shape[0] - 1), np.log(safe_div(radius[:-1], radius[-1])).sum())

    def estimate_gamma(self, alpha, Z, w):
        """
        Estimate risk index gamma
        :param self:
        :param alpha:
        :param Z:
        :param w:
        :return:
        """
        return (1 / (Z.shape[0] - 1)) * np.power(np.clip(w * Z[:-1].T, 0.0, np.inf), alpha).sum()

    def loss(self, w, alpha, Z, x):
        # minimize allocation risk
        gamma = self.estimate_gamma(alpha, Z, w)
        # if the experts mean returns are low and you have no options, you can choose fiat
        return self.rc * gamma + w[-1] * ((x.mean()) * x.var()) ** 2

    def update(self, b, x, alpha, Z):
        """
        Update portfolio weights to satisfy constraint b * x <= eps
        and minimize distance to previous portfolio.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        x_mean = np.mean(x)
        portvar = np.dot(b, x)

        change = abs((portvar + x[np.argmax(abs(x - x_mean))]) / 2)

        lam = np.clip(safe_div(change - self.eps, np.linalg.norm(x - x_mean) ** 2), 0.0, 1e6)

        # update portfolio
        b += lam * (x - x_mean)

        # # project it onto simplex
        b = simplex_proj(b) * (1 - self.eta) + self.eta * self.crp

        if self.rc > 0:
            # Extreme risk index
            # simplex constraints
            cons = [
                {'type': 'eq', 'fun': lambda w: w.sum() - 1},  # Simplex region
                {'type': 'ineq', 'fun': lambda w: w}  # Positive bound
            ]

            if self.mpc < 1:
                # Maximum position concentration constraint
                cons.append({'type': 'ineq', 'fun': lambda w: self.mpc - np.linalg.norm(w[:-1], ord=np.inf)})

            # Minimize loss starting from adjusted portfolio
            b = minimize(self.loss, b, args=(alpha, Z, x + 1), constraints=cons)['x']

        # Return best portfolio
        return np.clip(b, 0, 1)  # Truncate small errors

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.init:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = array_normalize(action)
            self.init = True

        if self.step:
            b = self.get_portfolio_vector(obs)
            x = self.predict(obs)
            # return self.update(prev_posit, price_relative)
            R, Z = self.polar_returns(obs)
            alpha = self.estimate_alpha(R)
            self.last_port = self.update(b, x, alpha, Z)
            return self.last_port

        else:
            return self.crp

    def set_params(self, **kwargs):
        if 'eps' in kwargs:
            self.eps = kwargs['eps']
        if 'eta' in kwargs:
            self.eta = kwargs['eta']


class KAMAMR(STMR):
    def __repr__(self):
        return "KAMAMR"

    def __init__(self, eps=0.02, window=3, rebalance=True, activation=simplex_proj, fiat="BTC", name="STMR"):
        """
        :param sensitivity: float: Sensitivity parameter. Lower is more sensitive.
        """
        super().__init__(fiat=fiat, name=name)
        self.eps = eps
        self.window = window
        self.activation = activation
        if rebalance:
            self.reb = -2
        else:
            self.reb = -1

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        prices = obs.xs('open', level=1, axis=1).astype(np.float64)
        mu = prices.apply(tl.KAMA, timeperiod=self.window, raw=True).iloc[-1].values

        price_relative = np.append(safe_div(mu, prices.iloc[-1].values) - 1, [0.0])

        return price_relative

    def set_params(self, **kwargs):
        self.eps = kwargs['eps']
        self.window = int(kwargs['window'])


# Portfolio optimization
class TCO(APrioriAgent):
    """
    Transaction cost optimization for online portfolio selection

    Reference:
        B. Li and J. Wang
        http://ink.library.smu.edu.sg/cgi/viewcontent.cgi?article=4761&context=sis_research
    """
    def __repr__(self):
        return "TCO"

    def __init__(self, factor=models.price_relative, toff=0.1, optimize_factor=True, rebalance=True, fiat="BTC", name="TCO"):
        """
        :param window: integer: Lookback window size.
        :param eps: float: Threshold value for updating portfolio.
        """
        super().__init__(fiat=fiat, name=name)
        self.toff = toff
        self.factor = factor
        self.optimize_factor = optimize_factor
        if rebalance:
            self.reb = -2
        else:
            self.reb = -1

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        # price_predict = np.empty(obs.columns.levels[0].shape[0] - 1, dtype=np.float64)
        # for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
        #     price_predict[key] = np.float64(obs[symbol].open.iloc[-self.window:].mean() /
        #                                     (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon))
        prev_posit = self.get_portfolio_vector(obs, index=-1) + 1
        factor_posit = self.factor(obs) + 1
        return safe_div(factor_posit, prev_posit)

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=self.reb)
            price_prediction = self.predict(obs)
            return self.update(prev_posit, price_prediction)
        else:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x >= eps
        and minimize distance to previous weights.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        vt = safe_div(x, np.dot(b, x))
        vt_mean = np.mean(vt)
        # update portfolio
        b += np.sign(vt - vt_mean) * np.clip(abs(vt - vt_mean) - self.toff, 0.0, np.inf)

        # project it onto simplex
        return simplex_proj(b)

    def set_params(self, **kwargs):
        self.toff = kwargs['toff']
        if self.optimize_factor:
            self.factor.set_params(**kwargs)


class Anticor(APrioriAgent):
    """ Anticor (anti-correlation) is a heuristic portfolio selection algorithm.
    It adopts the consistency of positive lagged cross-correlation and negative
    autocorrelation to adjust the portfolio. Eventhough it has no known bounds and
    hence is not considered to be universal, it has very strong empirical results.
    Reference:
        A. Borodin, R. El-Yaniv, and V. Gogan.  Can we learn to beat the best stock, 2005.
        http://www.cs.technion.ac.il/~rani/el-yaniv-papers/BorodinEG03.pdf
    """

    def __repr__(self):
        return "Anticor"

    def __init__(self, window=30, fiat="BTC"):
        """
        :param window: Window parameter.
        """
        super().__init__(fiat=fiat)
        self.window = window

    def predict(self, obs):
        """

        :param obs:
        :return:
        """
        price_log1 = np.empty((self.window - 2, obs.columns.levels[0].shape[0] - 1), dtype='f')
        price_log2 = np.empty((self.window - 2, obs.columns.levels[0].shape[0] - 1), dtype='f')
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_log1[:, key] = obs[symbol].open.iloc[-2 * self.window + 1:-self.window].rolling(2).apply(
                lambda x: np.log10(safe_div(x[-1], x[-2]))).dropna().values.T
            price_log2[:, key] = obs[symbol].open.iloc[-self.window + 1:].rolling(2).apply(
                lambda x: np.log10(safe_div(x[-1], x[-2]))).dropna().values.T
        return price_log1, price_log2

    def rebalance(self, obs):
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-1)[:-1]
            factor = self.predict(obs)
            return self.update(prev_posit, *factor)
        else:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)

    @staticmethod
    def zero_to_inf(vec):
        return np.vectorize(lambda x: np.inf if np.allclose(x, [0.0]) else x)(vec)

    def update(self, b, lx1, lx2):
        mean2 = lx2.mean(axis=0)
        std1 = self.zero_to_inf(lx1.std(axis=0))
        std2 = self.zero_to_inf(lx2.std(axis=0))

        corr = np.matmul(((lx1 - lx1.mean(axis=0)) / std1).T, (lx2 - mean2) / std2)
        claim = np.zeros_like(corr)

        for i in range(corr.shape[0]):
            for j in range(corr.shape[1]):
                if i == j: continue
                else:
                    if mean2[i] > mean2[j] and corr[i, j] > 0:
                        # Correlation matrix
                        claim[i, j] += corr[i, j]
                        # autocorrelation
                        if corr[i, i] < 0:
                            claim[i, j] += abs(corr[i, i])
                        if corr[j, j] < 0:
                            claim[i, j] += abs(corr[j, j])

        # calculate transfer
        transfer = claim * 0.
        for i in range(corr.shape[0]):
            total_claim = sum(claim[i, :])
            if total_claim != 0:
                transfer[i, :] = b[i] * safe_div(claim[i, :], total_claim)

        b += np.sum(transfer, axis=0) - np.sum(transfer, axis=1)

        return np.append(simplex_proj(b), [0.0])

    def set_params(self, **kwargs):
        self.window = int(kwargs['window'])


# Modern Portfolio Theory
class MeanVariance(APrioriAgent):

    def __repr__(self):
        return "Modern Portfolio Theory"

    def __init__(self, factor=models.price_relative, fiat="BTC", name='TangentPortfolio'):
        """
        :param window: Window parameter.
        """
        super().__init__(fiat=fiat, name=name)
        self.factor = factor
        self.fiat = fiat
        self.init = False

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        return self.factor(obs).iloc[-1]

    def update(self, cov_mat, exp_rets):
        raise NotImplementedError("You should overwrite this method in the child class.")

    def rebalance(self, obs):
        raise NotImplementedError("You should overwrite this method in the child class.")

    def set_params(self, **kwargs):
        self.target_return = kwargs['target_return']


class TangencyPortfolio(MeanVariance):
    """
    Computes a tangency portfolio, i.e. a maximum Sharpe ratio portfolio.
    """

    def __repr__(self):
        return "Tangency Portfolio"

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.init:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = array_normalize(action)
            self.init = True

        if self.step:
            x = self.predict(obs)
            price_relative = obs.xs('open', level=1, axis=1).apply(lambda x: ta.ROCR(x, timeperiod=1),
                                                                   raw=True).fillna(1.0)
            cov_mat = price_relative.cov()
            return self.update(cov_mat, x)
        else:
            return self.crp

    def update(self, cov_mat, exp_rets):
        """
         Note: As the Sharpe ratio is not invariant with respect
         to leverage, it is not possible to construct non-trivial
         market neutral tangency portfolios. This is because for
         a positive initial Sharpe ratio the sharpe grows unbound
         with increasing leverage.

         Parameters
         ----------
         cov_mat: pandas.DataFrame
             Covariance matrix of asset returns.
         exp_rets: pandas.Series
             Expected asset returns (often historical returns).
         allow_short: bool, optional
             If 'False' construct a long-only portfolio.
             If 'True' allow shorting, i.e. negative weights.

         Returns
         -------
         weights: pandas.Series
             Optimal asset weights.
         """
        if not isinstance(cov_mat, pd.DataFrame):
            raise ValueError("Covariance matrix is not a DataFrame")

        if not isinstance(exp_rets, pd.Series):
            raise ValueError("Expected returns is not a Series")

        if not cov_mat.index.equals(exp_rets.index):
            raise ValueError("Indices do not match")

        n = len(cov_mat)

        P = opt.matrix(cov_mat.values)
        q = opt.matrix(0.0, (n, 1))

        # Constraints Gx <= h
        # exp_rets*x >= 1 and x >= 0
        G = opt.matrix(np.vstack((-exp_rets.values,
                                  -np.identity(n))))
        h = opt.matrix(np.vstack((-1.0,
                                  np.zeros((n, 1)))))

        # Solve
        optsolvers.options['show_progress'] = False
        sol = optsolvers.qp(P, q, G, h)

        if sol['status'] != 'optimal':
            warnings.warn("Convergence problem")

        weights = np.append(np.squeeze(sol['x']), [0.0])

        # Rescale weights, so that sum(weights) = 1
        weights /= weights.sum()
        return weights


class Markowitz(MeanVariance):
    """
    Markowitz portfolio optimization
    """
    def __repr__(self):
        return "Markowitz Portfolio"

    def __init__(self, factor=models.price_relative, target_return=0.0025, fiat="BTC", name='Markowitz'):
        """
        :param window: Window parameter.
        """
        super().__init__(fiat=fiat, name=name)
        self.target_return = target_return
        self.factor = factor
        self.fiat = fiat
        self.init = False

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.init:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = array_normalize(action)
            self.init = True

        if self.step:
            x = self.predict(obs)
            # x[self.fiat] = 1 * (1 - x.std())
            price_relative = obs.xs('open', level=1, axis=1).apply(lambda x: ta.ROCR(x, timeperiod=1),
                                                                   raw=True).fillna(1.0)
            # price_relative[self.fiat] = 1 * (1 - price_relative.std(axis=1))
            cov_mat = price_relative.cov()
            return self.update(cov_mat, x, self.target_return)
        else:
            return self.crp

    def update(self, cov_mat, exp_rets, target_ret):
        """
        Computes a Markowitz portfolio.

        Parameters
        ----------
        cov_mat: pandas.DataFrame
            Covariance matrix of asset returns.
        exp_rets: pandas.Series
            Expected asset returns (often historical returns).
        target_ret: float
            Target return of portfolio.
        allow_short: bool, optional
            If 'False' construct a long-only portfolio.
            If 'True' allow shorting, i.e. negative weights.
        market_neutral: bool, optional
            If 'False' sum of weights equals one.
            If 'True' sum of weights equal zero, i.e. create a
                market neutral portfolio (implies allow_short=True).

        Returns
        -------
        weights: pandas.Series
            Optimal asset weights.
        """
        if not isinstance(cov_mat, pd.DataFrame):
            raise ValueError("Covariance matrix is not a DataFrame")

        if not isinstance(exp_rets, pd.Series):
            raise ValueError("Expected returns is not a Series")

        if not isinstance(target_ret, float):
            raise ValueError("Target return is not a float")

        if not cov_mat.index.equals(exp_rets.index):
            raise ValueError("Indices do not match")

        n = len(cov_mat)

        P = opt.matrix(cov_mat.values)
        q = opt.matrix(0.0, (n, 1))

        # Constraints Gx <= h
        # exp_rets*x >= target_ret and x >= 0
        G = opt.matrix(np.vstack((-exp_rets.values,
                                  -np.identity(n))))
        h = opt.matrix(np.vstack((-target_ret,
                                  +np.zeros((n, 1)))))

        # Constraints Ax = b
        # sum(x) = 1
        A = opt.matrix(1.0, (1, n))

        b = opt.matrix(1.0)

        # Solve
        optsolvers.options['show_progress'] = False
        sol = optsolvers.qp(P, q, G, h, A, b)

        if sol['status'] != 'optimal':
            warnings.warn("Convergence problem")

        # Put weights into a labeled series
        weights = np.append(np.squeeze(sol['x']), [0.0])
        return weights


# Risk optimization
class ERI(APrioriAgent):
    """
    This algorithm uses Extreme Risk Index to optimize a constant rebalance portfolio
    References:
        Extreme Risk Index:
        https://arxiv.org/pdf/1505.04045.pdf
    """
    def __repr__(self):
        return "Extreme Risk Index"

    def __init__(self, window=300, k=0.1, mpc=0.3, beta=0.999, fiat="BTC", name='ERI'):
        super().__init__(fiat=fiat, name=name)
        self.window = window - 1
        self.k = k
        self.mpc = mpc
        self.beta = beta

        # Extreme risk index
        self.cons = [
            {'type': 'eq', 'fun': lambda w: w.sum() - 1}, # Simplex region
            {'type': 'ineq', 'fun': lambda w: w}, # Positive bound
            {'type': 'ineq', 'fun': lambda w: self.mpc - np.linalg.norm(w, ord=np.inf)}
            # Maximum position concentration constraint
        ]

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        prices = obs.xs('open', level=1, axis=1)

        log_returns = np.hstack([prices.rolling(2).apply(
            lambda x: np.log(safe_div(x[-1], x[-2]))).dropna().values, np.zeros((self.window, 1))])

        return log_returns

    # Pareto Extreme Risk Index
    @staticmethod
    def estimate_alpha(R):
        return safe_div((R.shape[0] - 1), np.log(safe_div(R[:-1], R[-1])).sum())

    @staticmethod
    def estimate_gamma(alpha, Z, w):
        return (1 / (Z.shape[0] - 1)) * np.power(np.clip(w * Z[:-1].T, 0.0, np.inf), alpha).sum()

    def loss(self, w, alpha, Z, b):
        return self.estimate_gamma(alpha, Z, w) + np.linalg.norm(b - w) ** 2

    def update(self, b, x):
        last_x = x[-1, :]

        R, Z = risk.polar_returns(-x, self.k)
        alpha = self.estimate_alpha(R)

        self.r_hat = self.beta * self.r_hat + (1 - self.beta) * last_x

        cons = self.cons + [{'type': 'eq', 'fun': lambda w:
            np.dot(w, self.r_hat) - np.clip(0.001, 0.0, self.r_hat.max() / np.sqrt(2))}]

        b = minimize(
            self.loss,
            b,
            args=(alpha, Z, b),
            constraints=cons,
            options={'maxiter': 3333},
            tol=1e-7,
            bounds=tuple((0,1) for _ in range(b.shape[0]))
        )

        # Log variables
        self.log['r_hat'] = "%.4f, %.4f, %.4f" % (self.r_hat.min(), self.r_hat.mean(), self.r_hat.max())
        self.log['alpha'] = "%.2f" % alpha
        self.log['gamma'] = "%.8f" % b['fun']
        self.log['CC'] = "%.2f" % np.power(b['x'], 2).sum() ** -1
        self.log['nit'] = "%d" % b['nit']
        self.log['k'] = "%.2f" % self.k
        self.log['mpc'] = "%.2f" % self.mpc
        self.log['beta'] = "%.4f" % self.beta

        return b['x'] # Truncate small errors

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if not self.step:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = self.b = array_normalize(action)
            self.r_hat = np.zeros(n_pairs)
            return self.crp

        self.b = self.update(self.b, self.predict(obs))
        return self.b


## Agent Pipeline
class Pipeline(APrioriAgent):
    def __repr__(self):
        return "Pipeline"

    def __init__(self, factor, risk, fiat="BTC", name='Pipeline'):
        super().__init__(fiat=fiat, name=name)
        self.factor = factor
        self.risk = risk

    def rebalance(self, obs):
        """
         Performs portfolio rebalance within environment
         :param obs: pandas DataFrame: Environment observation
         :return: numpy array: Portfolio vector
         """
        if not self.step:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.crp = self.b = array_normalize(action)
            return self.crp

        self.factor.b = self.b
        self.risk.b = self.factor.rebalance(obs)
        self.b = self.risk.rebalance(obs)

        self.factor.step = self.risk.step = self.step

        return self.b
