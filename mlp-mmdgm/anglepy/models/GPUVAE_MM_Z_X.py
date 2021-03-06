'''
Code for mmDGM
Author: Chongxuan Li (chongxuanli1991@gmail.com)
Co-author: Tianlin Shi
Version = '1.0'
'''

import sys, os
import pdb
import numpy as np
import theano
import theano.tensor as T
import collections as C
import anglepy as ap
import anglepy.ndict as ndict
import color
from anglepy.misc import lazytheanofunc

import math, inspect

#import theano.sandbox.cuda.rng_curand as rng_curand

def shared32(x, name=None, borrow=False):
    return theano.shared(np.asarray(x, dtype='float32'), name=name, borrow=borrow)

def cast32(x):
    return T.cast(x, dtype='float32')

'''
Fully connected deep variational auto-encoder (VAE_Z_X)
'''

class GPUVAE_MM_Z_X(ap.GPUVAEModel):
    
    def __init__(self, get_optimizer, n_x, n_y, n_hidden_q, n_z, n_hidden_p, nonlinear_q='tanh', nonlinear_p='tanh', type_px='bernoulli', type_qz='gaussianmarg', type_pz='gaussianmarg', prior_sd=1, init_sd=1e-2, var_smoothing=0, n_mixture=50, c=10, ell=1, average_activation = 0.1, sparsity_weight = 3):
        self.constr = (__name__, inspect.stack()[0][3], locals())
        self.n_x = n_x
        self.n_y = n_y
        self.n_hidden_q = n_hidden_q
        self.n_z = n_z
        self.n_hidden_p = n_hidden_p
        self.dropout = False
        self.nonlinear_q = nonlinear_q
        self.nonlinear_p = nonlinear_p
        self.type_px = type_px
        self.type_qz = type_qz
        self.type_pz = type_pz
        self.prior_sd = prior_sd
        self.var_smoothing = var_smoothing
        self.n_mixture = n_mixture
        self.c = c
        self.ell = ell
        self.average_activation = average_activation
        self.sparsity_weight = sparsity_weight
        
        if os.environ.has_key('c'):
          self.c = float(os.environ['c'])
        if os.environ.has_key('ell'):
          self.ell = float(os.environ['ell'])
        self.sv = 0
        if os.environ.has_key('sv'):
          self.sv = int(os.environ['sv'])
          color.printBlue('apply supervision from layer ' + str(self.sv+1) + ' to end.')
        self.super_to_mean = False
        if os.environ.has_key('super_to_mean') and bool(int(os.environ['super_to_mean'])) == True:
          self.super_to_mean = True
          color.printBlue('apply supervision to z_mean.')
        self.train_residual = False
        if os.environ.has_key('train_residual') and bool(int(os.environ['train_residual'])) == True:
          self.train_residual = True
          color.printBlue('Train residual wrt prior instead of the whole model.')
        self.Lambda = 0
        if os.environ.has_key('Lambda'):
          self.Lambda = float(os.environ['Lambda'])
        self.sigma_square = 1
        if os.environ.has_key('sigma_square'):
          self.sigma_square = float(os.environ['sigma_square'])
        if os.environ.has_key('dropout'):
          self.dropout = bool(int(os.environ['dropout']))
        color.printBlue('c = ' + str(self.c) + ' , ell = ' + str(self.ell) + ' , sigma_square = ' + str(self.sigma_square))

        
        # Init weights
        v, w = self.init_w(1e-2)
        for i in v: v[i] = shared32(v[i])
        for i in w: w[i] = shared32(w[i])
        if not self.super_to_mean:
            W = shared32(np.zeros((sum(n_hidden_q[self.sv:])+1, n_y)))
            #print 'apply supervision from', self.sv+1, ' to end.'
        else:
            W = shared32(np.zeros((n_z+1, n_y)))
            #print 'apply supervison to z_mean'
        
        self.v = v
        self.v['W'] = W
        #print 'dimension of the prediction model: ', self.v['W'].get_value().shape
        self.w = w
        
        super(GPUVAE_MM_Z_X, self).__init__(get_optimizer)
    
    def factors(self, x, z, A):
        
        v = self.v         # parameters of recognition model. 
        w = self.w         # parameters of generative model. 
        
        '''
        z is unused
        x['x'] is the data
        
        The names of dict z[...] may be confusing here: the latent variable z is not included in the dict z[...],
        but implicitely computed from epsilon and parameters in w.

        z is computed with g(.) from eps and variational parameters
        let logpx be the generative model density: log p(x|z) where z=g(.)
        let logpz be the prior of Z plus the entropy of q(z|x): logp(z) + H_q(z|x)
        So the lower bound L(x) = logpx + logpz
        
        let logpv and logpw be the (prior) density of the parameters
        '''
        
        # Compute q(z|x)
        hidden_q = [x['x']]
        hidden_q_s = [x['x']]
        
        def f_softplus(x): return T.log(T.exp(x) + 1)# - np.log(2)
        def f_rectlin(x): return x*(x>0)
        def f_rectlin2(x): return x*(x>0) + 0.01 * x
        nonlinear = {'tanh': T.tanh, 'sigmoid': T.nnet.sigmoid, 'softplus': f_softplus, 'rectlin': f_rectlin, 'rectlin2': f_rectlin2}
        nonlinear_q = nonlinear[self.nonlinear_q]
        nonlinear_p = nonlinear[self.nonlinear_p]
        
        #rng = rng_curand.CURAND_RandomStreams(0)
        import theano.tensor.shared_randomstreams
        rng = theano.tensor.shared_randomstreams.RandomStreams(0)
        
        # TOTAL HACK
        #hidden_q.append(nonlinear_q(T.dot(v['scale0'], A) * T.dot(w['out_w'].T, hidden_q[-1]) + T.dot(v['b0'], A)))
        #hidden_q.append(nonlinear_q(T.dot(v['scale1'], A) * T.dot(w['w1'].T, hidden_q[-1]) + T.dot(v['b1'], A)))
        for i in range(len(self.n_hidden_q)):
            hidden_q.append(nonlinear_q(T.dot(v['w'+str(i)], hidden_q[-1]) + T.dot(v['b'+str(i)], A)))
            hidden_q_s.append(T.nnet.sigmoid(T.dot(v['w'+str(i)], hidden_q_s[-1]) + T.dot(v['b'+str(i)], A)))
            if self.dropout:
                hidden_q[-1] *= 2. * (rng.uniform(size=hidden_q[-1].shape, dtype='float32') > .5)
                hidden_q_s[-1] *= 2. * (rng.uniform(size=hidden_q_s[-1].shape, dtype='float32') > .5)
        
        '''
        print 'mm_model'
        for (d, xx) in x.items():
          print d
        '''
        
        #print 'x', x['mean_prior'].type
        #print 'T', (T.dot(v['mean_w'], hidden_q[-1]) + T.dot(v['mean_b'], A)).type
        
        if not self.train_residual:
            q_mean = T.dot(v['mean_w'], hidden_q[-1]) + T.dot(v['mean_b'], A)
        else:
            q_mean = x['mean_prior'] + T.dot(v['mean_w'], hidden_q[-1]) + T.dot(v['mean_b'], A)
        #q_mean = T.dot(v['mean_w'], hidden_q[-1]) + T.dot(v['mean_b'], A)
        
        if self.type_qz == 'gaussian' or self.type_qz == 'gaussianmarg':
            q_logvar = T.dot(v['logvar_w'], hidden_q[-1]) + T.dot(v['logvar_b'], A)
        else: raise Exception()
        
        ell = cast32(self.ell)
        self.param_c = shared32(0)
        sv = self.sv

        a_a = cast32(self.average_activation)
        s_w = cast32(self.sparsity_weight)
        
        def activate():
            res = 0
            if self.super_to_mean:
                lenw = len(v['W'].get_value())
                res += T.dot(v['W'][:-1,:].T, q_mean)
                res += T.dot(v['W'][lenw-1:lenw,:].T, A)
            else:
                lenw = len(v['W'].get_value())
                for (hi, hidden) in enumerate(hidden_q[1+sv:]):
                    res += T.dot(v['W'][sum(self.n_hidden_q[sv:sv+hi]):sum(self.n_hidden_q[sv:sv+hi+1]),:].T, hidden)
                res += T.dot(v['W'][lenw-1:lenw,:].T, A)
            return res
        predy = T.argmax(activate(), axis=0)

        # function for distribution q(z|x)
        theanofunc = lazytheanofunc('warn', mode='FAST_RUN')
        self.dist_qz['z'] = theanofunc([x['x'], x['mean_prior']] + [A], [q_mean, q_logvar])
        self.dist_qz['hidden'] = theanofunc([x['x'], x['mean_prior']] + [A], hidden_q[1:])
        self.dist_qz['predy'] = theanofunc([x['x'], x['mean_prior']] + [A], predy)
        
        # compute cost (posterior regularization).
        true_resp = (activate() * x['y']).sum(axis=0, keepdims=True)
        T.addbroadcast(true_resp, 0)

        cost = self.param_c * (ell * (1-x['y']) + activate() - true_resp).max(axis=0).sum()  \
                        + self.Lambda * (v['W'] * v['W']).sum()
        
        # compute the sparsity penalty
        sparsity_penalty = 0
        for i in range(1, len(hidden_q_s)):
            sparsity_penalty += (a_a*T.log(a_a/(hidden_q_s[i].mean(axis=1))) + (1-a_a)*T.log((1-a_a)/(1-(hidden_q_s[i].mean(axis=1))))).sum(axis=0)
        sparsity_penalty *= s_w

        # Compute virtual sample
        eps = rng.normal(size=q_mean.shape, dtype='float32')
        _z = q_mean + T.exp(0.5 * q_logvar) * eps
        
        # Compute log p(x|z)
        hidden_p = [_z]
        for i in range(len(self.n_hidden_p)):
            hidden_p.append(nonlinear_p(T.dot(w['w'+str(i)], hidden_p[-1]) + T.dot(w['b'+str(i)], A)))
            if self.dropout:
                hidden_p[-1] *= 2. * (rng.uniform(size=hidden_p[-1].shape, dtype='float32') > .5)
        
        if self.type_px == 'bernoulli':
            p = T.nnet.sigmoid(T.dot(w['out_w'], hidden_p[-1]) + T.dot(w['out_b'], A))
            _logpx = - T.nnet.binary_crossentropy(p, x['x'])
            self.dist_px['x'] = theanofunc([_z] + [A], p)
        elif self.type_px == 'gaussian':
            x_mean = T.dot(w['out_w'], hidden_p[-1]) + T.dot(w['out_b'], A)
            x_logvar = T.dot(w['out_logvar_w'], hidden_p[-1]) + T.dot(w['out_logvar_b'], A)
            _logpx = ap.logpdfs.normal2(x['x'], x_mean, x_logvar)
            self.dist_px['x'] = theanofunc([_z] + [A], [x_mean, x_logvar])
        elif self.type_px == 'bounded01':
            x_mean = T.nnet.sigmoid(T.dot(w['out_w'], hidden_p[-1]) + T.dot(w['out_b'], A))
            x_logvar = T.dot(w['out_logvar_b'], A)
            _logpx = ap.logpdfs.normal2(x['x'], x_mean, x_logvar)
            # Make it a mixture between uniform and Gaussian
            w_unif = T.nnet.sigmoid(T.dot(w['out_unif'], A))
            _logpx = T.log(w_unif + (1-w_unif) * T.exp(_logpx))
            self.dist_px['x'] = theanofunc([_z] + [A], [x_mean, x_logvar])
        else: raise Exception("")
            
        # Note: logpx is a row vector (one element per sample)
        logpx = T.dot(shared32(np.ones((1, self.n_x))), _logpx) # logpx = log p(x|z,w)
        
        # log p(z) (prior of z)
        if self.type_pz == 'gaussianmarg':
            if not self.train_residual:
                logpz = -0.5 * (np.log(2 * np.pi * self.sigma_square) + ((q_mean-x['mean_prior'])**2 + T.exp(q_logvar))/self.sigma_square).sum(axis=0, keepdims=True)
            else:
                logpz = -0.5 * (np.log(2 * np.pi * self.sigma_square) + (q_mean**2 + T.exp(q_logvar))/self.sigma_square).sum(axis=0, keepdims=True)
        elif self.type_pz == 'gaussian':
            logpz = ap.logpdfs.standard_normal(_z).sum(axis=0, keepdims=True)
        elif self.type_pz == 'mog':
            pz = 0
            for i in range(self.n_mixture):
                pz += T.exp(ap.logpdfs.normal2(_z, T.dot(w['mog_mean'+str(i)], A), T.dot(w['mog_logvar'+str(i)], A)))
            logpz = T.log(pz).sum(axis=0, keepdims=True) - self.n_z * np.log(float(self.n_mixture))
        elif self.type_pz == 'laplace':
            logpz = ap.logpdfs.standard_laplace(_z).sum(axis=0, keepdims=True)
        elif self.type_pz == 'studentt':
            logpz = ap.logpdfs.studentt(_z, T.dot(T.exp(w['logv']), A)).sum(axis=0, keepdims=True)
        else:
            raise Exception("Unknown type_pz")
        
        # loq q(z|x) (entropy of z)
        if self.type_qz == 'gaussianmarg':
            logqz = - 0.5 * (np.log(2 * np.pi) + 1 + q_logvar).sum(axis=0, keepdims=True)
        elif self.type_qz == 'gaussian':
            logqz = ap.logpdfs.normal2(_z, q_mean, q_logvar).sum(axis=0, keepdims=True)
        else: raise Exception()
                        
        # [new part] Fisher divergence of latent variables
        if self.var_smoothing > 0:
            dlogq_dz = T.grad(logqz.sum(), _z) # gives error when using gaussianmarg instead of gaussian
            dlogp_dz = T.grad((logpx + logpz).sum(), _z)
            FD = 0.5 * ((dlogq_dz - dlogp_dz)**2).sum(axis=0, keepdims=True)
            # [end new part]
            logqz -= self.var_smoothing * FD
        
        # Note: logpv and logpw are a scalars
        if True:
            def f_prior(_w, prior_sd=self.prior_sd):
                return ap.logpdfs.normal(_w, 0, prior_sd).sum()
        else:
            def f_prior(_w, prior_sd=self.prior_sd):
                return ap.logpdfs.standard_laplace(_w / prior_sd).sum()
            
        return logpx, logpz, logqz, cost, sparsity_penalty
    
    # Generate epsilon from prior
    def gen_eps(self, n_batch):
        z = {'eps': np.random.standard_normal(size=(self.n_z, n_batch)).astype('float32')}
        return z
    
    # Generate variables
    def gen_xz_prior(self, x, z, mean_prior, sigma_square, n_batch):
        
        x, z = ndict.ordereddicts((x, z))
        
        A = np.ones((1, n_batch)).astype(np.float32)
        for i in z: z[i] = z[i].astype(np.float32)
        for i in x: x[i] = x[i].astype(np.float32)
        tmp = np.random.standard_normal(size=(self.n_z, n_batch)).astype(np.float32)
        z['z'] = tmp * np.sqrt(sigma_square) + mean_prior
        
        if self.type_px == 'bernoulli':
            x['x'] = self.dist_px['x'](*([z['z']] + [A]))
        elif self.type_px == 'bounded01' or self.type_px == 'gaussian':
            x_mean, x_logvar = self.dist_px['x'](*([z['z']] + [A]))
            if not x.has_key('x'):
                x['x'] = np.random.normal(x_mean, np.exp(x_logvar/2))
                if self.type_px == 'bounded01':
                    x['x'] = np.maximum(np.zeros(x['x'].shape), x['x'])
                    x['x'] = np.minimum(np.ones(x['x'].shape), x['x'])
                    
        else: raise Exception("")
        
        return x
    
    # Generate variables
    def gen_xz(self, x, z, n_batch):
        
        x, z = ndict.ordereddicts((x, z))
        
        A = np.ones((1, n_batch)).astype(np.float32)
        for i in z: z[i] = z[i].astype(np.float32)
        for i in x: x[i] = x[i].astype(np.float32)
        
        _z = {}

        # If x['x'] was given but not z['z']: generate z ~ q(z|x)
        if x.has_key('x') and not z.has_key('z'):

            q_mean, q_logvar = self.dist_qz['z'](*([x['x'], x['mean_prior']] + [A]))
            q_hidden = self.dist_qz['hidden'](*([x['x'], x['mean_prior']] + [A]))
            predy = self.dist_qz['predy'](*([x['x'], x['mean_prior']] + [A]))

            _z['mean'] = q_mean
            _z['logvar'] = q_logvar
            _z['hidden'] = q_hidden
            _z['predy'] = predy
            
            # Require epsilon
            if not z.has_key('eps'):
                eps = self.gen_eps(n_batch)['eps']
            
            z['z'] = q_mean + np.exp(0.5 * q_logvar) * eps
            
        elif not z.has_key('z'):
            if self.type_pz in ['gaussian','gaussianmarg']:
                z['z'] = np.random.standard_normal(size=(self.n_z, n_batch)).astype(np.float32)
            elif self.type_pz == 'laplace':
                z['z'] = np.random.laplace(size=(self.n_z, n_batch)).astype(np.float32)
            elif self.type_pz == 'studentt':
                z['z'] = np.random.standard_t(np.dot(np.exp(self.w['logv'].get_value()), A)).astype(np.float32)
            elif self.type_pz == 'mog':
                i = np.random.randint(self.n_mixture)
                loc = np.dot(self.w['mog_mean'+str(i)].get_value(), A)
                scale = np.dot(np.exp(.5*self.w['mog_logvar'+str(i)].get_value()), A)
                z['z'] = np.random.normal(loc=loc, scale=scale).astype(np.float32)
            else:
                raise Exception('Unknown type_pz')
        # Generate from p(x|z)
        
        if self.type_px == 'bernoulli':
            p = self.dist_px['x'](*([z['z']] + [A]))
            _z['x'] = p
            if not x.has_key('x'):
                x['x'] = np.random.binomial(n=1,p=p)
        elif self.type_px == 'bounded01' or self.type_px == 'gaussian':
            x_mean, x_logvar = self.dist_px['x'](*([z['z']] + [A]))
            _z['x'] = x_mean
            if not x.has_key('x'):
                x['x'] = np.random.normal(x_mean, np.exp(x_logvar/2))
                if self.type_px == 'bounded01':
                    x['x'] = np.maximum(np.zeros(x['x'].shape), x['x'])
                    x['x'] = np.minimum(np.ones(x['x'].shape), x['x'])
        
        else: raise Exception("")
        
        return x, z, _z
    
    def gen_xz_prior11(self, x, z, mean_prior, sigma_square, n_batch):
        
        x, z = ndict.ordereddicts((x, z))
        A = np.ones((1, n_batch)).astype(np.float32)
        z['z'] = mean_prior.astype(np.float32)
        
        if self.type_px == 'bernoulli':
            x['x'] = self.dist_px['x'](*([z['z']] + [A]))
        elif self.type_px == 'bounded01' or self.type_px == 'gaussian':
            x_mean, x_logvar = self.dist_px['x'](*([z['z']] + [A]))
            if not x.has_key('x'):
                x['x'] = np.random.normal(x_mean, np.exp(x_logvar/2))
                if self.type_px == 'bounded01':
                    x['x'] = np.maximum(np.zeros(x['x'].shape), x['x'])
                    x['x'] = np.minimum(np.ones(x['x'].shape), x['x'])
                    
        else: raise Exception("")
        
        return x
        
    def variables(self):
        
        z = {}

        # Define observed variables 'x'
        x = {'x': T.fmatrix('x'), 'mean_prior': T.fmatrix('mean_prior'), 'y': T.fmatrix('y'), }
        #x = {'x': T.fmatrix('x'), 'y': T.fmatrix('y'), }
        
        return x, z
    
    def init_w(self, std=1e-2):
        
        def rand(size):
            if len(size) == 2 and size[1] > 1:
                return np.random.normal(0, 1, size=size) / np.sqrt(size[1])
            return np.random.normal(0, std, size=size)
        
        v = {}
        #v['scale0'] = np.ones((self.n_hidden_q[0], 1))
        #v['scale1'] = np.ones((self.n_hidden_q[0], 1))
        v['w0'] = rand((self.n_hidden_q[0], self.n_x))
        v['b0'] = rand((self.n_hidden_q[0], 1))
        for i in range(1, len(self.n_hidden_q)):
            v['w'+str(i)] = rand((self.n_hidden_q[i], self.n_hidden_q[i-1]))
            v['b'+str(i)] = rand((self.n_hidden_q[i], 1))
        
        v['mean_w'] = rand((self.n_z, self.n_hidden_q[-1]))
        v['mean_b'] = rand((self.n_z, 1))
        if self.type_qz in ['gaussian','gaussianmarg']:
            v['logvar_w'] = np.zeros((self.n_z, self.n_hidden_q[-1]))
        v['logvar_b'] = np.zeros((self.n_z, 1))
        
        w = {}

        if self.type_pz == 'mog':
            for i in range(self.n_mixture):
                w['mog_mean'+str(i)] = rand((self.n_z, 1))
                w['mog_logvar'+str(i)] = rand((self.n_z, 1))
        if self.type_pz == 'studentt':
            w['logv'] = np.zeros((self.n_z, 1))
        
        
        if len(self.n_hidden_p) > 0:
            w['w0'] = rand((self.n_hidden_p[0], self.n_z))
            w['b0'] = rand((self.n_hidden_p[0], 1))
            for i in range(1, len(self.n_hidden_p)):
                w['w'+str(i)] = rand((self.n_hidden_p[i], self.n_hidden_p[i-1]))
                w['b'+str(i)] = rand((self.n_hidden_p[i], 1))
            w['out_w'] = rand((self.n_x, self.n_hidden_p[-1]))
            w['out_b'] = np.zeros((self.n_x, 1))
            if self.type_px == 'gaussian':
                w['out_logvar_w'] = rand((self.n_x, self.n_hidden_p[-1]))
                w['out_logvar_b'] = np.zeros((self.n_x, 1))
            if self.type_px == 'bounded01':
                w['out_logvar_b'] = np.zeros((self.n_x, 1))
                w['out_unif'] = np.zeros((self.n_x, 1))
                
        else:
            w['out_w'] = rand((self.n_x, self.n_z))
            w['out_b'] = np.zeros((self.n_x, 1))
            if self.type_px == 'gaussian':
                w['out_logvar_w'] = rand((self.n_x, self.n_z))
                w['out_logvar_b'] = np.zeros((self.n_x, 1))
            if self.type_px == 'bounded01':
                w['out_logvar_b'] = np.zeros((self.n_x, 1))
                w['out_unif'] = np.zeros((self.n_x, 1))

        return v, w
