import sys, os, time

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
from rospy.numpy_msg import numpy_msg
from std_msgs.msg import Float32, Int32

from slam_msgs.msg import PlotPosteriorResult, PlotPosteriorAction
from slam_msgs.msg import SamplePosteriorResult, SamplePosteriorAction
from slam_msgs.msg import MinibatchTrainingAction, MinibatchTrainingResult, MinibatchTrainingGoal

import actionlib

import numpy as np

import warnings
import time
from pathlib import Path
import json

class SVGP(VariationalGP):

    def __init__(self, num_inducing):

        # variational distribution and strategy
        # NOTE: we put random normal dumby inducing points
        # here, which we'll change in self.fit
        vardist = CholeskyVariationalDistribution(num_inducing)
        varstra = VariationalStrategy(
            self,
            torch.randn((num_inducing, 2)),
            vardist,
            learn_inducing_locations=True
        )
        VariationalGP.__init__(self, varstra)

        # kernel — implemented in self.forward
        self.mean = ConstantMean()
        self.cov = MaternKernel(ard_num_dims=2)
        # self.cov = GaussianSymmetrizedKLKernel()
        self.cov = ScaleKernel(self.cov, ard_num_dims=2)

    def forward(self, input):
        m = self.mean(input)
        v = self.cov(input)
        return MultivariateNormal(m, v)

model = SVGP(10)
likelihood = GaussianLikelihood()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
likelihood.to(device).float()
model.to(device).float()

path = "/tmp/my.fifo"
try:
    os.mkfifo(path)
except:
    pass
try:
    fifo = open(path, "w")
except Exception as e:
    print (e)
    sys.exit()

x = 0
time_total = 0.
while x < 100:
    time_start = time.time()
    fifo = open(path, "w")
    fifo.write(str(model.state_dict()))
    #  fifo.write(str(model.state_dict()))
    fifo.flush()
    time_total += time.time() - time_start
    #  print ("Sending:", str(model.state_dict()))
    print ("Sending:", str(x))
    print(model.state_dict())
    fifo.close()
    x+=1
    time.sleep(0.001)
print ("Closing ", x)
print("Avg time fifo ", time_total/100.)
try:
    os.unlink(fifo)
except:
    pass

fname = "svpg.pth"

x = 0
time_total = 0.
while x < 100:
    time_start = time.time()
    torch.save({'model' : model.state_dict()}, fname)
    time_total += time.time() - time_start
    x+=1

print("Avg time saving ", time_total/100.)
print ("Closing ", x)

x = 0
time_total = 0.
while x < 100:
    time_start = time.time()
    cp = torch.load(fname)
    time_total += time.time() - time_start
    x+=1
print("Avg time loading ", time_total/100.)
print ("Closing ", x)


#  time_start = time.time()
#  cp = torch.load(fname)
#  print("Time loading ", time.time() - time_start)
#  model.load_state_dict(cp['model'])


