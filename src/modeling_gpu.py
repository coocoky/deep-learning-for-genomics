#!/usr/bin/env python

from __future__ import print_function

import theano
import theano.tensor as T

import numpy

from lasagne.layers import (
    InputLayer, DenseLayer,
    get_output,
    get_all_params, get_all_param_values, set_all_param_values
)
from parmesan.layers.sample import SimpleSampleLayer, SampleLayer

from lasagne.nonlinearities import identity, sigmoid, rectify, softmax, softplus, tanh

from parmesan.distributions import (
    log_normal, log_bernoulli,
    kl_normal2_stdnormal
)

from lasagne import updates

import data

from time import time
from aux import convertTimeToString

class VariationalAutoEncoder(object):
    def __init__(self, feature_shape, latent_size, hidden_structure, reconstruction_distribution = None):
        
        # Setup
        
        super(VariationalAutoEncoder, self).__init__()
        
        print("Setting up model.")
        print("    feature size: {}".format(feature_shape))
        print("    latent size: {}".format(latent_size))
        print("    hidden structure: {}".format(", ".join(map(str, hidden_structure))))
        if type(reconstruction_distribution) == str:
            print("    reconstruction distribution: " + reconstruction_distribution)
        else:
            print("    reconstruction distribution: custom")
        print("")
        
        self.feature_shape = feature_shape
        self.latent_size = latent_size
        self.hidden_structure = hidden_structure
        
        symbolic_x = T.matrix('x')
        symbolic_z = T.matrix('z')
        symbolic_learning_rate = T.scalar("epsilon")
        
        self.number_of_epochs_trained = 0
        self.learning_curves = {
            "training": {
                "lower bound": [],
                "log p(x|z)": [],
                "KL divergence": []
            },
            "validation": {
                "lower bound": [],
                "log p(x|z)": [],
                "KL divergence": []
            }
        }
        
        if reconstruction_distribution:
            
            if type(reconstruction_distribution) == str:
                reconstruction_distribution = \
                    reconstruction_distributions[reconstruction_distribution]
            
            self.x_parameters = reconstruction_distribution["parameters"]
            self.reconstruction_activation_functions = \
                reconstruction_distribution["activation functions"]
            self.expectedNegativeReconstructionError = reconstruction_distribution["function"]
            self.meanOfReconstructionDistribution = reconstruction_distribution["mean"]
            self.preprocess = reconstruction_distribution["preprocess"]
        else:
            # Use a Gaussian distribution as standard
            self.x_parameters = ["mu", "sigma"]
            self.reconstruction_activation_functions = {
                "mu": identity,
                "sigma": identity
            }
            self.expectedNegativeReconstructionError = lambda x, x_theta, eps = 0.0: \
                log_normal(x, x_theta["mu"], x_theta["sigma"], eps)
            self.meanOfReconstructionDistribution = lambda x_theta: x_theta["mu"]
            self.preprocess = lambda x: x
        
        # Models
    
        ## Recognition model q(z|x)
    
        l_enc_in = InputLayer(shape = (None, feature_shape), name = "ENC_INPUT")
        l_enc = l_enc_in
        
        for i, hidden_size in enumerate(hidden_structure):
            l_enc = DenseLayer(l_enc, num_units = hidden_size, nonlinearity = rectify, name = 'ENC_DENSE{:d}'.format(i + 1))
        
        l_z_mu = DenseLayer(l_enc, num_units = latent_size, nonlinearity = None, name = 'ENC_Z_MU')
        l_z_log_var = DenseLayer(l_enc, num_units = latent_size, nonlinearity = lambda x: T.clip(x, -10, 10), name = 'ENC_Z_LOG_VAR')
        
        # Sample a latent representation z \sim q(z|x) = N(mu(x), logvar(x))
        l_z = SimpleSampleLayer(mean = l_z_mu, log_var = l_z_log_var, name = "ENC_SAMPLE")
        
        self.encoder = l_z
        
        ## Generative model p(x|z)
        
        l_dec_in = InputLayer(shape = (None, latent_size), name = "DEC_INPUT")
        l_dec = l_dec_in
        
        for i, hidden_size in enumerate(reversed(hidden_structure)):
            l_dec = DenseLayer(l_dec, num_units = hidden_size, nonlinearity = rectify, name = 'DEC_DENSE{:d}'.format(len(hidden_structure) - i))
        
        l_x_theta = {
            p: DenseLayer(l_dec, num_units = feature_shape,
                nonlinearity = self.reconstruction_activation_functions[p],
                name = 'DEC_X_' + p.upper()) for p in self.x_parameters
        }
        
        self.decoder = {p: l_x_theta[p] for p in self.x_parameters}
        
        ## Get outputs from models
        
        ## Training outputs
        z_train, z_mu_train, z_log_var_train = get_output(
            [l_z, l_z_mu, l_z_log_var], {l_enc_in: symbolic_x}, deterministic = False)
        x_theta_train = get_output([l_x_theta[p] for p in self.x_parameters], {l_dec_in: z_train},
            deterministic = False)
        x_theta_train = {p: o for p, o in zip(self.x_parameters, x_theta_train)}
        
        ## Evaluation outputs
        z_eval, z_mu_eval, z_log_var_eval = get_output(
            [l_z, l_z_mu, l_z_log_var], {l_enc_in: symbolic_x}, deterministic = True)
        x_theta_eval = get_output([l_x_theta[p] for p in self.x_parameters], {l_dec_in: z_eval},
            deterministic = True)
        x_theta_eval = {p: o for p, o in zip(self.x_parameters, x_theta_eval)}
        
        ## Sample outputs
        
        x_theta_sample = get_output([l_x_theta[p] for p in self.x_parameters], {l_dec_in: symbolic_z},
            deterministic = True)
        x_theta_sample = {p: o for p, o in zip(self.x_parameters, x_theta_sample)}
        
        # Likelihood
        
        lower_bound_train, log_p_x_train, KL__train = \
            self.lowerBound(symbolic_x, x_theta_train, z_mu_train, z_log_var_train)
        lower_bound_eval, log_p_x_eval, KL__eval = \
            self.lowerBound(symbolic_x, x_theta_eval, z_mu_eval, z_log_var_eval)
        
        all_parameters = get_all_params([l_z] + [l_x_theta[p] for p in self.x_parameters], trainable = True)
        
        print("Parameters to train:")
        for parameter in all_parameters:
            print("    {}: {}".format(parameter, parameter.get_value().shape))
        
        # Let Theano do its magic and get all the gradients we need for training
        all_gradients = T.grad(-lower_bound_train, all_parameters)

        # Set the update function for parameters. The Adam optimizer works really well with VAEs.
        update_expressions = updates.adam(all_gradients, all_parameters,
            learning_rate = symbolic_learning_rate)
        
        self.x_train = theano.shared(numpy.zeros([1, 1]),
            theano.config.floatX, borrow = True)
        self.x_eval = theano.shared(numpy.zeros([1, 1]),
            theano.config.floatX, borrow = True)
        
        symbolic_batch_size = T.iscalar('batch_size')
        symbolic_batch_index = T.iscalar('batch_index')
        batch_slice = slice(symbolic_batch_index * symbolic_batch_size,
            (symbolic_batch_index + 1) * symbolic_batch_size)
        
        self.f_train = theano.function(
            inputs = [symbolic_batch_index, symbolic_batch_size, symbolic_learning_rate],
            outputs = [lower_bound_train, log_p_x_train, KL__train],
            givens = {symbolic_x: self.x_train[batch_slice]},
            updates = update_expressions)
        
        self.f_eval = theano.function(
            inputs = [],
            outputs = [lower_bound_eval, log_p_x_eval, KL__eval],
            givens = {symbolic_x: self.x_eval})
        
        self.f_z = theano.function(inputs = [symbolic_x], outputs = [z_eval])
        
        self.f_sample = theano.function(
            inputs = [symbolic_z],
            outputs = [x_theta_sample[p] for p in self.x_parameters])
        
        self.f_recon = theano.function(
            inputs = [symbolic_x],
            outputs = [x_theta_eval[p] for p in self.x_parameters])
    
    def lowerBound(self, x, x_theta, z_mu, z_log_var):
        #note that we sum the latent dimension and mean over the samples
        log_px_given_z = self.expectedNegativeReconstructionError(x, x_theta, eps = 1e-6).sum(axis = 1).mean()
        KL_qp = kl_normal2_stdnormal(z_mu, z_log_var).sum(axis = 1).mean()
        LL = - KL_qp + log_px_given_z
        return LL, log_px_given_z, KL_qp
    
    def train(self, training_set, validation_set = None, N_epochs = 50, batch_size = 100,
        learning_rate = 1e-3):
        
        N = training_set.shape[0]
        
        x_train = self.preprocess(training_set).astype(theano.config.floatX)
        x_valid = self.preprocess(validation_set).astype(theano.config.floatX)
        
        training_string = "Training model for {}".format(N_epochs)
        if self.number_of_epochs_trained > 0:
            training_string += " additional"
        training_string += " epoch{}".format("s" if N_epochs > 1 else "")
        training_string += " with a learning rate of {:.3g}".format(learning_rate)
        training_string += "."
        print(training_string)
        
        LL_train, logpx_train, KL_train = [], [], []
        LL_valid, logpx_valid, KL_valid = [], [], []
        
        training_start = time()
        
        for epoch in range(self.number_of_epochs_trained,
            self.number_of_epochs_trained + N_epochs):
            
            epoch_start = time()
            
            numpy.random.shuffle(x_train)
            self.x_train.set_value(x_train)
            
            for i in range(0, N, batch_size):
                out = self.f_train(i, batch_size, learning_rate)
            
            self.x_eval.set_value(x_train)
            out = self.f_eval()
            LL_train += [out[0]] 
            logpx_train += [out[1]]
            KL_train += [out[2]]
            
            evaluation_string = "    Training set:   lower bound: {:.5g}, log p(x|z): {:.5g}, KL divergence: {:.5g}.".format(float(out[0]), float(out[1]), float(out[2]))
            
            if x_valid is not None:
                self.x_eval.set_value(x_valid)
                out = self.f_eval()
                LL_valid += [out[0]]
                logpx_valid += [out[1]]
                KL_valid += [out[2]]
                
                evaluation_string += "\n    Validation set: lower bound: {:.5g}, log p(x|z): {:.5g}, KL divergence: {:.5g}.".format(float(out[0]), float(out[1]), float(out[2]))
            
            epoch_duration = time() - epoch_start
                
            print("Epoch {:2d} ({}):".format(epoch + 1, convertTimeToString(epoch_duration)))
            print(evaluation_string)
        
        training_duration = time() - training_start
        
        self.number_of_epochs_trained += N_epochs
        
        self.learning_curves["training"]["lower bound"] += LL_train
        self.learning_curves["training"]["log p(x|z)"] += logpx_train
        self.learning_curves["training"]["KL divergence"] += KL_train
        
        self.learning_curves["validation"]["lower bound"] += LL_valid
        self.learning_curves["validation"]["log p(x|z)"] += logpx_valid
        self.learning_curves["validation"]["KL divergence"] += KL_valid
        
        print("Training finished with a total of {} epoch{} after {}.".format(self.number_of_epochs_trained, "s" if N_epochs > 1 else "", convertTimeToString(training_duration)))
    
    def evaluate(self, test_set):
        
        x_test = theano.shared(self.preprocess(test_set),
            theano.config.floatX, borrow = True)
        
        self.x_eval.set_value(x_test)
        lower_bound_test, _, _ = self.f_eval(x_test)
        
        print("Lower bound for test set: {:.4g}.".format(float(lower_bound_test)))
        
        z_eval = self.f_z(x_test)[0]
        
        x_theta_sample = self.f_sample(numpy.random.normal(size = (100,
            self.latent_size)).astype('float32'))
        x_theta_sample = {p: o for p, o in zip(self.x_parameters, x_theta_sample)}
        x_sample = self.meanOfReconstructionDistribution(x_theta_sample)
        
        x_test_recon = self.f_recon(x_test)
        x_test_recon = {p: o for p, o in zip(self.x_parameters, x_test_recon)}
        x_test_recon["mean"] = self.meanOfReconstructionDistribution(x_test_recon)
        
        metrics = {
            "LL_test": lower_bound_test
        }
        
        return x_test, x_test_recon, z_eval, x_sample, metrics
    
    def save(self, name, metadata = None):
        
        model = {
            "feature shape": self.feature_shape,
            "latent size": self.latent_size,
            "hidden structure": self.hidden_structure,
            "encoder": get_all_param_values(self.encoder),
            "decoder": {
                p: get_all_param_values(self.decoder[p]) for p in self.x_parameters
            },
            "number of epochs trained": self.number_of_epochs_trained,
            "learning curves": self.learning_curves,
        }
        
        if metadata:
            model["metadata"] = metadata
        
        model_name = name
        
        data.saveModel(model, model_name)
    
    def load(self, model_name):
        
        model = data.loadModel(model_name)
        
        set_all_param_values(self.encoder, model["encoder"])
        
        for p in self.x_parameters:
            set_all_param_values(self.decoder[p], model["decoder"][p])
        
        self.number_of_epochs_trained = model["number of epochs trained"]
        self.learning_curves = model["learning curves"]

def log_negative_binomial(x, p, log_r, eps = 0.0, approximation = "simple"):
    """
    Compute log pdf of a negative binomial distribution with success probability p and number of failures, r, until the experiment is stopped, at values x.
    
    A simple variation of Stirling's approximation is used: log x! = x log x - x.
    """
    
    x = T.clip(x, eps, x)
    
    p = T.clip(p, eps, 1.0 - eps)
    
    r = T.exp(log_r)
    r = T.clip(r, eps, r)
    
    y = T.gammaln(x + r) - T.gammaln(x + 1) - T.gammaln(r) \
        + x * T.log(p) + r * T.log(1 - p)
    
    return y

def meanOfNegativeBinomialDistribution(p, log_r):
    return p * numpy.exp(log_r) / (1 - p)

def log_zero_inflated_poisson(x, pi, log_lambda, eps = 0.0, approximation = "simple"):
    """
    Compute log pdf of a zero-inflated Poisson distribution with success probability pi and number of failures, r, until the experiment is stopped, at values x.
    
    A simple variation of Stirling's approximation is used: log x! = x log x - x.
    """
    
    x = T.clip(x, eps, x)
    
    pi = T.clip(pi, eps, 1.0 - eps)
    
    lambda_ = T.exp(log_lambda)
    lambda_ = T.clip(lambda_, eps, lambda_)
    
    y_0 = T.log(pi + (1 - pi) * T.exp(-lambda_))
    y_1 = T.log(1 - pi) + x * log_lambda - lambda_ - T.gammaln(x + 1)
    # y_1 = T.log(1 - pi) + x * T.log(lambda_) - lambda_ - T.gammaln(x + 1)
    
    y = T.eq(x, eps) * y_0 + T.gt(x, eps) * y_1
    
    return y

reconstruction_distributions = {
    "negative_binomial": {
        "parameters": ["p", "log_r"],
        "activation functions": {
            # "p": sigmoid,
            "p": lambda x: T.clip(sigmoid(x), 0, 0.99),
            "log_r": lambda x: T.clip(x, -10, 10)
        },
        "function": lambda x, x_theta, eps = 0.0: \
            log_negative_binomial(x, x_theta["p"], x_theta["log_r"], eps),
        "mean": lambda x_theta: \
            meanOfNegativeBinomialDistribution(x_theta["p"], x_theta["log_r"]),
        "preprocess": lambda x: x
    },
    
    "bernoulli": {
        "parameters": ["p"],
        "activation functions": {
            "p": sigmoid,
        },
        "function": lambda x, x_theta, eps = 0.0: \
            log_bernoulli(x, x_theta["p"], eps),
        "mean": lambda x_theta: x_theta["p"],
        # TODO Consider switching to Bernouilli sampling
        "preprocess": lambda x: (x != 0).astype('float32')
    },
    
    "zero_inflated_poisson": {
        "parameters": ["pi", "log_lambda"],
        "activation functions": {
            "pi": sigmoid,
            "log_lambda": lambda x: T.clip(x, -10, 10)
        },
        "function": lambda x, x_theta, eps = 0.0: \
            log_zero_inflated_poisson(x, x_theta["pi"], x_theta["log_lambda"], eps),
        "mean": lambda x_theta: (1 - x_theta["pi"]) * numpy.exp(x_theta["log_lambda"]),
        "preprocess": lambda x: x
    }
}

if __name__ == '__main__':
    (training_set, training_headers), (validation_set, validation_headers), \
        (test_set, test_headers) = data.loadCountData("sample")
    feature_size = training_set.shape[1]
    model = VariationalAutoEncoder(feature_size, latent_size = 2, hidden_structure = [5], reconstruction_distribution = "negative_binomial")
    model.train(training_set, validation_set, N_epochs = 1, batch_size = 1)
    model.save("test")
    model.load("test")
    model.evaluate(test_set)