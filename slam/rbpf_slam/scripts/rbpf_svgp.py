#!/usr/bin/env python3

import torch, numpy as np, tqdm, matplotlib.pyplot as plt
from gpytorch.models import VariationalGP, ExactGP
from gpytorch.variational import CholeskyVariationalDistribution, VariationalStrategy
from gpytorch.means import ConstantMean
from gpytorch.kernels import MaternKernel, ScaleKernel, GaussianSymmetrizedKLKernel, InducingPointKernel
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.distributions import MultivariateNormal
from gpytorch.mlls import VariationalELBO, PredictiveLogLikelihood, ExactMarginalLogLikelihood
from gpytorch.test.utils import least_used_cuda_device
import gpytorch.settings
#from convergence import ExpMAStoppingCriterion
from gp_mapping.convergence import ExpMAStoppingCriterion
import matplotlib.pyplot as plt

import rospy
from sensor_msgs.msg import PointCloud2
import sensor_msgs.point_cloud2 as pc2

from slam_msgs.msg import PlotPosteriorResult, PlotPosteriorAction
from slam_msgs.msg import SamplePosteriorResult, SamplePosteriorAction
from slam_msgs.msg import MinibatchTrainingAction, MinibatchTrainingResult, MinibatchTrainingGoal

import actionlib

import numpy as np

import warnings
import time

class SVGP_Particle(VariationalGP):

    def __init__(self, particle_id):
        
        self.particle_id = particle_id

        # Number of inducing points
        num_inducing = rospy.get_param("~svgp_num_ind_points", 100)
        assert isinstance(num_inducing, int)
        self.s = num_inducing

        # variational distribution and strategy
        # NOTE: we put random normal dumby inducing points
        # here, which we'll change in self.fit
        vardist = CholeskyVariationalDistribution(self.s)
        varstra = VariationalStrategy(
            self,
            torch.randn((self.s, 2)),
            vardist,
            learn_inducing_locations=True
        )
        VariationalGP.__init__(self, varstra)

        # kernel — implemented in self.forward
        self.mean = ConstantMean()
        self.cov = MaternKernel(ard_num_dims=2)
        # self.cov = GaussianSymmetrizedKLKernel()
        self.cov = ScaleKernel(self.cov, ard_num_dims=2)
        
        # likelihood
        self.likelihood = GaussianLikelihood()

        # hardware allocation
        # TODO: optimize device allocation
        # with least_used_cuda_device():
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.likelihood.to(self.device).float()
        self.to(self.device).float()


    def setup_svgp(self):

        self.storage_path = rospy.get_param("~results_path")
        self.count_training = 0
        
        # AS for plotting results
        plot_gp_name = rospy.get_param("~plot_gp_server")
        self._as_plot = actionlib.SimpleActionServer("/particle_" + str(self.particle_id) + plot_gp_name, PlotPosteriorAction, 
                                                execute_cb=self.plot_posterior, auto_start = False)
        self._as_plot.start()

        # AS for expected meas
        sample_gp_name = rospy.get_param("~sample_gp_server")
        self._as_sample = actionlib.SimpleActionServer("/particle_" + str(self.particle_id) + sample_gp_name, SamplePosteriorAction, 
                                                execute_cb=self.sample_posterior, auto_start = False)
        self._as_sample.start()

        self.training = False
        self.plotting = False
        self.sampling = False

        # Remove Qt out of main thread warning (use with caution)
        warnings.filterwarnings("ignore")

        # AS for minibath training data from RBPF
        mb_gp_name = rospy.get_param("~minibatch_gp_server")
        self.ac_mb = actionlib.SimpleActionClient(mb_gp_name, MinibatchTrainingAction)
        self.ac_mb.wait_for_server()

        # Set up SVGP optimization
        self.mb_size = rospy.get_param("~svgp_minibatch_size", 1000)
        self.lr = rospy.get_param("~svpg_learning_rate", 1e-1)
        self.rtol = rospy.get_param("~svpg_rtol", 1e-4)
        self.n_window = rospy.get_param("~svpg_n_window", 100)
        self.auto = rospy.get_param("~svpg_auto_stop", False)
        self.verbose = rospy.get_param("~svpg_verbose", True)

        mll = VariationalELBO(self.likelihood, self, self.mb_size, combine_terms=True)
        opt = torch.optim.Adam(self.parameters(),lr=float(self.lr))
        # opt = torch.optim.SGD(self.parameters(),lr=learning_rate)

        # convergence criterion
        if self.auto: self.criterion = ExpMAStoppingCriterion(rel_tol=float(self.rtol), 
                                                    minimize=True, n_window=self.n_window)
        # Toggle training mode
        self.train()
        self.likelihood.train()
        self.loss = list()
        self.iterations = 0

        # Subscription to GP inducing points from RBPF
        ip_top = rospy.get_param("~inducing_points_top")
        rospy.Subscriber(ip_top, PointCloud2, self.ip_cb, queue_size=1)
        self.inducing_points_received = False

        print("Particle ", self.particle_id, " set up")
        # print("torch.cuda.memory_allocated: %fGB"%(torch.cuda.memory_allocated(self.device)/1024/1024/1024))
        # print("torch.cuda.memory_reserved: %fGB"%(torch.cuda.memory_reserved(self.device)/1024/1024/1024))
        # print("torch.cuda.max_memory_reserved: %fGB"%(torch.cuda.max_memory_reserved(self.device)/1024/1024/1024))
        # rospy.spin()

        return(mll, opt)

    def forward(self, input):
        m = self.mean(input)
        v = self.cov(input)
        return MultivariateNormal(m, v)

    def ip_cb(self, ip_cloud):

        if not self.inducing_points_received:
            # Store beams as array of 3D points
            wp_locations = []
            for p in pc2.read_points(ip_cloud, 
                                    field_names = ("x", "y", "z"), skip_nans=True):
                wp_locations.append(p)
            wp_locations = np.asarray(wp_locations)
            wp_locations = np.reshape(wp_locations, (-1,3))

            # Inducing points distributed on grid over survey area
            ip_locations = [
                np.linspace(min(wp_locations[:,0]), max(wp_locations[:,0]), round(np.sqrt(self.s))),
                np.linspace(min(wp_locations[:,1]), max(wp_locations[:,1]), round(np.sqrt(self.s)))
            ]
            inputst = np.meshgrid(*ip_locations)
            inputst = [_.flatten() for _ in inputst]
            inputst = np.vstack(inputst).transpose()
            self.variational_strategy.inducing_points.data = torch.from_numpy(inputst[:, 0:2]).to(self.device).float()

            self.inducing_points_received = True
            print("Particle ", self.particle_id, " starting training")


    def train_map(self, mll, opt):

        # Don't train until the inducing points from the RBPF node have been received
        if not self.inducing_points_received:
            rospy.loginfo_once("Waiting for inducing points")
            return

        if not self.plotting and not self.sampling:

            # Get beams for minibatch training as pcl
            goal = MinibatchTrainingGoal()
            goal.particle_id = self.particle_id
            goal.mb_size = self.mb_size
            self.ac_mb.send_goal(goal)
            self.ac_mb.wait_for_result()
            result = self.ac_mb.get_result()

            # If minibatch received from server
            if result.success:
                # Store beams as array of 3D points
                beams = np.asarray(list(pc2.read_points(result.minibatch, 
                                        field_names = ("x", "y", "z"), skip_nans=True)))
                beams = np.reshape(beams, (-1,3))

                self.training = True
                input = torch.from_numpy(beams[:, 0:2]).to(self.device).float()
                target = torch.from_numpy(beams[:,2]).to(self.device).float()

                # compute loss, compute gradient, and update
                loss = -mll(self(input), target)
                opt.zero_grad()
                loss.backward()
                opt.step()
                # print("GP ", self.particle_id, " training iteration ", self.iterations)
                self.training = False

                # verbosity and convergence check
                # if verbose:
                    # epochs.set_description('Loss {:.4f}'.format(loss.item()))
                self.loss.append(loss.detach().cpu().numpy())
                # if self.auto and self.criterion.evaluate(loss.detach()):
                #     break

                self.iterations += 1
            
            else:
                rospy.sleep(1)
            
        # print("Done with the training ", self.particle_id)
        # print("GP ", self.particle_id, " training iteration ", self.iterations)

    def sample_posterior(self, goal):

        beams = np.asarray(list(pc2.read_points(goal.ping, 
                                field_names = ("x", "y", "z"), skip_nans=True)))
        beams = np.reshape(beams, (-1,3)) 

        while not rospy.is_shutdown() and self.training:
            rospy.Rate(1).sleep()
            print("GP ", self.particle_id, " waiting for training before sampling")

        self.sampling = True
        mu, sigma = self.sample(np.asarray(beams)[:, 0:2])

        # Set action as success
        result = SamplePosteriorResult()
        result.mu = mu
        result.sigma = sigma
        self._as_sample.set_succeeded(result)
        self.sampling = False
        print("GP ", self.particle_id, " sampled")


    def plot_posterior(self, goal):

        # Flag to stop training
        self.plotting = True

        # Wait for training to stop
        while not rospy.is_shutdown() and self.training:
            rospy.Rate(1).sleep()
            print("GP ", self.particle_id, " waiting for training before plotting")

        beams = np.asarray(list(pc2.read_points(goal.pings, 
                        field_names = ("x", "y", "z"), skip_nans=True)))
        beams = np.reshape(beams, (-1,3)) 

        # Plot posterior and save it to image
        print("Plotting GP ", self.particle_id, " after ", self.iterations, " iterations")

        self.plotting = True
        self.plot(beams[:,0:2], beams[:,2], 
                     self.storage_path + 'particle_' + str(self.particle_id) 
                     + '_training.png',
                     n=50, n_contours=100 )

        # Set action as success
        result = PlotPosteriorResult()
        result.success = True
        self._as_plot.set_succeeded(result)
        # self.plotting = False
        print("GP posterior plotted ", self.particle_id)


    def sample(self, x):

        '''
        Samples the posterior at x
        x: (n,2) numpy array
        returns:
            mu: (n,) numpy array of predictive mean at x
            sigma: (n,) numpy array of predictive variance at x
        '''

        ## On your source code, call:
        # self.likelihood.eval()
        # self.eval()
        ## before using this function to toggle evaluation mode

        # sanity
        assert len(x.shape) == x.shape[1] == 2

        # sample posterior
        # TODO: fast_pred_var activates LOVE. Test performance on PF
        # https://towardsdatascience.com/gaussian-process-regression-using-gpytorch-2c174286f9cc
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            x = torch.from_numpy(x).to(self.device).float()
            dist = self.likelihood(self(x))
            return dist.mean.cpu().numpy(), dist.variance.cpu().numpy()

    def save_posterior(self, n, xlb, xub, ylb, yub, fname, verbose=True):

        '''
        Samples the GP posterior on a inform grid over the
        rectangular region defined by (xlb, xub) and (ylb, yub)
        and saves it as a pointcloud array.

        n: determines n² number of sampling locations
        xlb, xub: lower and upper bounds of x sampling locations
        ylb, yub: lower and upper bounds of y sampling locations
        fname: path to save array at (use .npy extension)
        '''

        # sanity
        assert('.npy' in fname)

        # toggle evaluation mode
        self.likelihood.eval()
        self.eval()
        torch.cuda.empty_cache()

        # posterior sampling locations
        inputs = [
            np.linspace(xlb, xub, n),
            np.linspace(ylb, yub, n)
        ]
        inputs = np.meshgrid(*inputs)
        inputs = [_.flatten() for _ in inputs]
        inputs = np.vstack(inputs).transpose()

        # split the array into smaller ones for memory
        inputs = np.split(inputs, 4000, axis=0)

        # compute the posterior for each batch
        means, variances = list(), list()
        with torch.no_grad():
            for i, input in enumerate(inputs):
                if verbose: print('Batch {}'.format(i))
                mean, variance = self.sample(input)
                means.append(mean)
                variances.append(variance)

        # assemble probabalistic pointcloud
        cloud = np.hstack((
            np.vstack(inputs),
            np.hstack(means).reshape(-1, 1),
            np.hstack(variances).reshape(-1, 1)
        ))

        # save it
        np.save(fname, cloud)

    def plot(self, inputs, targets, fname, n=80, n_contours=50):

        '''
        Plots:
            ax[0]: raw inputs and targets,
            ax[1]: posterior predictive mean,
            ax[2]: posterior predictive variance
        inputs: (n,2) numpy array of inputs
        output: (n,) numpy array of targets
        fname: path to save plot at (extension determines file type, e.g. .png or .pdf)
        n: determines n² number of sampling locations to plot GP posterior
        n_contours: number of contours to show output magnitude with
        '''

        # sanity
        assert inputs.shape[0] == targets.shape[0]
        assert inputs.shape[1] == 2

        # toggle evaluation mode
        self.likelihood.eval()
        self.eval()
        torch.cuda.empty_cache()

        # posterior sampling locations
        inputsg = [
            np.linspace(min(inputs[:,0]), max(inputs[:,0]), n),
            np.linspace(min(inputs[:,1]), max(inputs[:,1]), n)
        ]
        inputst = np.meshgrid(*inputsg)
        s = inputst[0].shape
        inputst = [_.flatten() for _ in inputst]
        inputst = np.vstack(inputst).transpose()
        inputst = torch.from_numpy(inputst).to(self.device).float()

        # sample
        with torch.no_grad():
            outputs = self(inputst)
            outputs = self.likelihood(outputs)
            mean = outputs.mean.cpu().numpy().reshape(s)
            variance = outputs.variance.cpu().numpy().reshape(s)

        # plot raw, mean, and variance
        fig, ax = plt.subplots(3, sharex=True, sharey=True)
        cr = ax[0].scatter(inputs[:,0], inputs[:,1], c=targets, cmap='viridis', s=0.4, edgecolors='none')
        cm = ax[1].contourf(*inputsg, mean, levels=n_contours)
        cv = ax[2].contourf(*inputsg, variance, levels=n_contours)
        indpts = self.variational_strategy.inducing_points.data.cpu().numpy()
        ax[2].plot(indpts[:,0], indpts[:,1], 'ko', markersize=1, alpha=0.2)

        # colorbars
        fig.colorbar(cr, ax=ax[0])
        fig.colorbar(cm, ax=ax[1])
        fig.colorbar(cv, ax=ax[2])

        # formatting
        ax[0].set_aspect('equal')
        ax[0].set_title('Raw data')
        ax[0].set_ylabel('$y~[m]$')
        ax[1].set_aspect('equal')
        ax[1].set_title('Mean')
        ax[1].set_ylabel('$y~[m]$')
        ax[2].set_aspect('equal')
        ax[2].set_title('Variance')
        ax[2].set_xlabel('$x~[m]$')
        ax[2].set_ylabel('$y~[m]$')
        plt.tight_layout()

        # save
        fig.savefig(fname, bbox_inches='tight', dpi=1000)

        self.plot_loss(self.storage_path + 'particle_' + str(self.particle_id) 
                        + '_training_loss.png')

        # Free up GPU mem
        del inputst
        torch.cuda.empty_cache()

    def plot_loss(self, fname):

        # plot
        fig, ax = plt.subplots(1)
        ax.plot(self.loss, 'k-')

        # format
        ax.set_xlabel('Iteration')
        ax.set_ylabel('ELBO')
        ax.set_yscale('log')
        plt.tight_layout()

        # save
        fig.savefig(fname, bbox_inches='tight', dpi=1000)
        
    def save(self, fname):
        torch.save(self.state_dict(), fname)

    @classmethod
    def load(cls, nind, fname):
        gp = cls(nind)
        gp.load_state_dict(torch.load(fname))
        return gp


if __name__ == '__main__':

    rospy.init_node('rbpf_svgp' , disable_signals=False)
    node_name = rospy.get_name()
    hdl_number = int(node_name.split('_')[2])
    particles_per_hdl = rospy.get_param("~num_particles_per_handler")

    try:
        particles = []
        particles_opt = []
        # Create the SVGP objects of this handler and store their ELBO optimizers
        for i in range(0, int(particles_per_hdl)):
            particles.append(SVGP_Particle(int(hdl_number)+i))
            # (mll, opt) = particle_map.setup_svgp()
            particles_opt.append(particles[i].setup_svgp())

        # In each round, call one minibatch training iteration per SVGP
        while not rospy.is_shutdown():
            for i in range(0, int(particles_per_hdl)):
                particles[i].train_map(particles_opt[i][0], particles_opt[i][1])  

        rospy.spin()
    except rospy.ROSInterruptException:
        rospy.logerr("Couldn't launch rbpf_svgp")