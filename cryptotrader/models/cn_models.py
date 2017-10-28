import numpy as np
np.random.seed(42)

import chainer
from chainer import report, Reporter, get_current_reporter
from chainer import functions as F
from chainer import links as L
from chainer import initializer
from chainer.initializers import Normal


eps = 1e-8

def phi(obs):
    """
    Feature extraction function
    """
    xp = chainer.cuda.get_array_module(obs)
    obs = xp.expand_dims(obs, 0)
    return obs.astype(np.float32)


def batch_states(states, xp, phi):
    """The default method for making batch of observations.

    Args:
        states (list): list of observations from an environment.
        xp (module): numpy or cupy
        phi (callable): Feature extractor applied to observations

    Return:
        the object which will be given as input to the model.
    """

    states = [phi(s) for s in states]
    return xp.asarray(states)


class LeCunNormal(initializer.Initializer):

    """Initializes array with scaled Gaussian distribution.
    Each element of the array is initialized by the value drawn
    independently from Gaussian distribution whose mean is 0,
    and standard deviation is
    :math:`scale \\times \\sqrt{\\frac{1}{fan_{in}}}`,
    where :math:`fan_{in}` is the number of input units.
    Reference: LeCun 98, Efficient Backprop
    http://yann.lecun.com/exdb/publis/pdf/lecun-98b.pdf
    Args:
        scale (float): A constant that determines the scale
            of the standard deviation.
        dtype: Data type specifier.
    """

    def __init__(self, scale=1.0, dtype=None):
        self.scale = scale
        super(LeCunNormal, self).__init__(dtype)

    def __call__(self, array):
        if self.dtype is not None:
            assert array.dtype == self.dtype
        fan_in, fan_out = initializer.get_fans(array.shape)
        s = self.scale * np.sqrt(1. / fan_in)
        Normal(s)(array)


class ProcessObs(chainer.Link):
    """
    Observations preprocessing / feature extraction layer
    """
    def __init__(self):
        super().__init__()
        # with self.init_scope():
        #     self.bn = L.BatchNormalization(self.out_channels)

    def __call__(self, x):
        xp = chainer.cuda.get_array_module(x)
        obs = []

        for i in [i for i in range(int(x.shape[-1]) - 1) if i % 6 == 0]:
            pair = []
            pair.append(xp.expand_dims(x[:,:,:, i + 1] / (x[:,:,:, i] + eps) - 1., -2))
            pair.append(xp.expand_dims(x[:,:,:, i + 2] / (x[:,:,:, i] + eps) - 1., -2))
            pair.append(xp.expand_dims(x[:,:,:, i + 3] / (x[:,:,:, i] + eps) - 1., -2))
            obs.append(xp.concatenate(pair, axis=1))

        # shape[batch_size, features, n_pairs, timesteps]
        # return self.bn(xp.concatenate(obs, axis=-2))
        return xp.concatenate(obs, axis=-2)


class PortfolioVector(chainer.Link):
    def __init__(self):
        super().__init__()

    def __call__(self, x):
        n_cols = int(x.shape[-1])
        n_pairs = int((n_cols - 1) / 6)

        xp = chainer.cuda.get_array_module(x)
        cv = np.zeros((1, n_pairs))
        for i, j in enumerate([i - 1 for i in range(1, n_cols) if (i % 6) == 0]):
            cv[0, i] = xp.expand_dims(x[:,:,-1, j] * x[:,:,-1, j - 2], -1)

        return chainer.Variable(xp.reshape(xp.concatenate(cv / (cv.sum() + x[:,:,-1, n_cols - 1]), axis=-1),
                                           [-1,1,n_pairs,1]))


class CashBias(chainer.Link):
    """
    Write me
    """
    def __init__(self):
        super().__init__()

    def __call__(self, x):
        xp = chainer.cuda.get_array_module(x)
        fiat = xp.ones([x.shape[0], x.shape[1], 1, 1], dtype='f') - F.sum(x, axis=2, keepdims=True)
        return F.concat([x, fiat], axis=-2)


class ConvBlock(chainer.Chain):
    """
    Write me
    """
    def __init__(self, in_channels, out_channels, ksize, pad=(0,0)):
        super().__init__()
        with self.init_scope():
            self.conv = L.Convolution2D(in_channels, out_channels, ksize, pad=pad,
                                        nobias=False, initialW=LeCunNormal())
            self.bn = L.BatchNormalization(out_channels)

    def __call__(self, x):
        h = self.conv(x)
        h = self.bn(h)
        return F.relu(h)


class VisionModel(chainer.Chain):
    """
    Write me
    """
    def __init__(self, timesteps, vn_number, pn_number):
        super().__init__()
        with self.init_scope():
            self.obs = ProcessObs()
            self.filt1 = ConvBlock(3, vn_number, (1, 3), (0, 1))
            self.filt2 = ConvBlock(3, vn_number, (1, 5), (0, 2))
            self.filt3 = ConvBlock(3, vn_number, (1, 7), (0, 3))
            self.filt4 = ConvBlock(3, vn_number, (1, 9), (0, 4))
            self.filt_out = ConvBlock(vn_number * 4, pn_number, (1, timesteps), (0, 0))

    def __call__(self, x):
        h = self.obs(x)
        h = F.concat([self.filt1(h), self.filt2(h), self.filt3(h), self.filt4(h)], axis=1)
        return self.filt_out(h)


class EIIE(chainer.Chain):
    """
    Write me
    """
    def __init__(self, timesteps, vn_number, pn_number):
        super().__init__()
        with self.init_scope():
            self.vision = VisionModel(timesteps, vn_number, pn_number)
            # self.portvec = PortfolioVector(input_shape)
            self.conv = L.Convolution2D(pn_number, 1, 1, 1, nobias=False, initialW=LeCunNormal())
            # self.cashbias = CashBias()

    def __call__(self, x):
        h = self.vision(x)
        # h = F.concat([h, self.portvec(x)], axis=1)
        h = self.conv(h)
        # h = self.cashbias(h)
        return h


# Train functions
def get_target(obs):
    n_cols = int(obs.shape[-1])
    n_pairs = int((n_cols - 1) / 6)
    target = np.zeros((1, n_pairs))
    for i, j in enumerate([i for i in range(n_cols - 1) if i % 6 == 0]):
        target[0, i] = np.expand_dims(obs[j + 3] / (obs[j] + 1e-8) - 1., -1)
    return target


def make_batch(env, batch_size):
    obs_batch = []
    target_batch = []
    for i in range(batch_size):
        # Choose some random index
        env.index = np.random.randint(high=env.data_length, low=env.obs_steps)
        # Get obs and target and append it to their batches
        obs = env.get_observation(True).astype(np.float32).values
        xp = chainer.cuda.get_array_module(obs)
        print(obs.shape)
        obs_batch.append(obs[:-1])
        target_batch.append(get_target(obs[-1]))

    obs_batch = batch_states(obs_batch, xp, phi)
    target_batch = np.swapaxes(batch_states(target_batch, xp, phi), 3, 2)

    return obs_batch, target_batch

def train_EIIE():
    pass