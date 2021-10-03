#!/usr/bin/env python3

# Standard dependencies
import os
import math

from numpy.core.fromnumeric import shape
import rospy
import random
import sys
import numpy as np
import tf2_ros
from scipy.spatial.transform import Rotation as rot
from scipy.ndimage import gaussian_filter1d

from geometry_msgs.msg import Pose, PoseArray, PoseWithCovarianceStamped
from geometry_msgs.msg import Transform, Quaternion
from nav_msgs.msg import Odometry
from std_msgs.msg import Float32, Header, Bool, Float32MultiArray, ByteMultiArray
from rospy_tutorials.msg import Floats
from rospy.numpy_msg import numpy_msg

from tf.transformations import quaternion_from_euler, euler_from_quaternion
from tf.transformations import translation_matrix, translation_from_matrix
from tf.transformations import quaternion_matrix, quaternion_from_matrix
from tf.transformations import rotation_matrix, rotation_from_matrix

from sensor_msgs.msg import PointCloud2, PointField
import sensor_msgs.point_cloud2 as pc2
from cv_bridge import CvBridge

# For sim mbes action client
import actionlib
from auv_2_ros.msg import MbesSimGoal, MbesSimAction, MbesSimResult
from rbpf_particle import Particle, matrix_from_tf, pcloud2ranges, pack_cloud, pcloud2ranges_full, matrix_from_pose
from resampling import residual_resample, naive_resample, systematic_resample, stratified_resample

# Auvlib
from auvlib.bathy_maps import base_draper
from auvlib.data_tools import csv_data, all_data, std_data

from scipy.ndimage.filters import gaussian_filter

# gpytorch
from bathy_gps import gp
import time 

# For large numbers 
from mpmath import mpf

class atree(): # ancestry tree
    def __init__(self, ID, parent, trajectory, observations):
        self.ID = ID
        self.parent = parent
        self.trajectory = trajectory
        self.observations = observations
        self.children = []

class rbpf_slam(object):

    def __init__(self):
        # Read necessary parameters
        self.pc = rospy.get_param('~particle_count', 10) # Particle Count
        self.map_frame = rospy.get_param('~map_frame', 'map') # map frame_id
        self.mbes_frame = rospy.get_param('~mbes_link', 'mbes_link') # mbes frame_id
        odom_frame = rospy.get_param('~odom_frame', 'odom')
        meas_model_as = rospy.get_param('~mbes_as', '/mbes_sim_server') # map frame_id
        self.beams_num = rospy.get_param("~num_beams_sim", 20)
        self.beams_real = rospy.get_param("~n_beams_mbes", 512)
        self.mbes_angle = rospy.get_param("~mbes_open_angle", np.pi/180. * 60.)
        self.n_inducing = rospy.get_param("~n_inducing", 300)
        self.storage_path = rospy.get_param("~data_path") #'/home/stine/catkin_ws/src/UWExploration/slam/rbpf_slam/data/results/'
        qz = rospy.get_param("~queue_size", 10) # to pub/sub to gp training
        self.l_max = rospy.get_param("~l_max", .5)
        self.gamma = rospy.get_param("~gamma", .8)
        self.th_reg = rospy.get_param("~th_reg", 80.)
        self.record_data = rospy.get_param("~record_data", 1)


        # Initialize tf listener
        tfBuffer = tf2_ros.Buffer()
        tf2_ros.TransformListener(tfBuffer)
        
        # Read covariance values
        meas_std = float(rospy.get_param('~measurement_std', 0.01))
        cov_string = rospy.get_param('~motion_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        motion_cov = list(map(float, cov_list))

        cov_string = rospy.get_param('~init_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        init_cov = list(map(float, cov_list))

        cov_string = rospy.get_param('~resampling_noise_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        self.res_noise_cov = list(map(float, cov_list))

        # Global variables
        self.pred_odom = None
        self.n_eff_filt = 0.
        self.n_eff_mask = [self.pc]*3
        self.mbes_history = []
        self.latest_mbes = PointCloud2()
        self.count_pings = 0
        self.prev_mbes = PointCloud2()
        self.poses = PoseArray()
        self.poses.header.frame_id = odom_frame
        self.avg_pose = PoseWithCovarianceStamped()
        self.avg_pose.header.frame_id = odom_frame
        self.targets = np.zeros((1,))
        self.firstFit = True
        self.one_time = True
        self.time2resample = False
        self.count_training = 0
        self.pw = [1.e-50] * self.pc # Start with almost zero weight
        # for ancestry tree
        self.observations = np.zeros((1,3)) 
        self.mapping= np.zeros((1,3)) 
        self.p_ID = 0
        self.tree_list = []
        self.time4regression = False
        self.n_from = 1
        self.ctr = 0

        # Nacho
        self.pings_since_training = 0

        # Initialize particle poses publisher
        pose_array_top = rospy.get_param("~particle_poses_topic", '/particle_poses')
        self.pf_pub = rospy.Publisher(pose_array_top, PoseArray, queue_size=10)

        # Initialize average of poses publisher
        avg_pose_top = rospy.get_param("~average_pose_topic", '/average_pose')
        self.avg_pub = rospy.Publisher(avg_pose_top, PoseWithCovarianceStamped, queue_size=10)

        # Expected meas of PF outcome at every time step
        pf_mbes_top = rospy.get_param("~average_mbes_topic", '/avg_mbes')
        self.pf_mbes_pub = rospy.Publisher(pf_mbes_top, PointCloud2, queue_size=1)

        stats_top = rospy.get_param('~pf_stats_top', 'stats')
        self.stats = rospy.Publisher(stats_top, numpy_msg(Floats), queue_size=10)

        mbes_pc_top = rospy.get_param("~particle_sim_mbes_topic", '/sim_mbes')
        self.pcloud_pub = rospy.Publisher(mbes_pc_top, PointCloud2, queue_size=10)
        
        # Load mesh
        svp_path = rospy.get_param('~sound_velocity_prof')
        mesh_path = rospy.get_param('~mesh_path')
        
        if svp_path.split('.')[1] != 'cereal':
            sound_speeds = csv_data.csv_asvp_sound_speed.parse_file(svp_path)
        else:
            sound_speeds = csv_data.csv_asvp_sound_speed.read_data(svp_path)  

        data = np.load(mesh_path)
        V, F, bounds = data['V'], data['F'], data['bounds'] 
        print("Mesh loaded")

        # Create draper
        self.draper = base_draper.BaseDraper(V, F, bounds, sound_speeds)
        self.draper.set_ray_tracing_enabled(False)            
        data = None
        V = None
        F = None
        bounds = None 
        sound_speeds = None
        print("draper created")
        print("Size of draper: ", sys.getsizeof(self.draper))        
 
        # Load GP
        gp_path = rospy.get_param("~gp_path", 'gp.path')
        self.gp = gp.SVGP.load(1000, gp_path)
        print("Size of GP: ", sys.getsizeof(self.gp))

        # Action server for MBES pings sim (necessary to be able to use UFO maps as well)
        sim_mbes_as = rospy.get_param('~mbes_sim_as', '/mbes_sim_server')
        self.as_ping = actionlib.SimpleActionServer('/mbes_sim_server', MbesSimAction, 
                                                    execute_cb=self.mbes_as_cb, auto_start=True)

        # Publish to record data
        # self.recordData2gp()
        train_gp_topic = rospy.get_param('~train_gp_topic', '/training_gps')
        self.gp_pub = rospy.Publisher(train_gp_topic, numpy_msg(Floats), queue_size=qz)
        # Subscribe to gp variance and mean
        rospy.Subscriber('/gp_meanvar', numpy_msg(Floats), self.gp_meanvar_cb, queue_size=qz)
        # Subscribe to when to save down trajectory
        rospy.Subscriber('/keyboard_trajectory', Bool, self.save_trajectory_cb, queue_size=1 )
        # Subscribe to keep on checking length scale
        # self.length_pub = rospy.Publisher('/length_scale', Bool, queue_size=1)
        rospy.Subscriber('/length_scale', numpy_msg(Floats), self.cb_lengthscale, queue_size=qz)
        # Subscribe to final particle index
        rospy.Subscriber('/final_gp_topic', Float32, self.save_final_gp_cb, queue_size=1)


        # Subscription to real mbes pings 
        mbes_pings_top = rospy.get_param("~mbes_pings_topic", 'mbes_pings')
        rospy.Subscriber(mbes_pings_top, PointCloud2, self.mbes_real_cb, queue_size=100)
        
        # Establish subscription to odometry message (intentionally last)
        odom_top = rospy.get_param("~odometry_topic", 'odom')
        rospy.Subscriber(odom_top, Odometry, self.odom_callback)

        # Create expected MBES beams directions
        angle_step = self.mbes_angle/self.beams_num
        self.beams_dir = []
        for i in range(0, self.beams_num):
            roll_step = rotation_matrix(-self.mbes_angle/2.
                                        + angle_step * i, (1,0,0))[0:3, 0:3]
            rot = roll_step[:,2]
            self.beams_dir.append(rot/np.linalg.norm(rot))
        
        # Shift for fast ray tracing in 2D
        beams = np.asarray(self.beams_dir)
        n_beams = len(self.beams_dir)
        self.beams_dir_2d = np.concatenate((beams[:,0].reshape(n_beams,1), 
                                            np.roll(beams[:, 1:3], 1, 
                                                    axis=1).reshape(n_beams,2)), axis=1)

        self.beams_dir_2d = np.array([1,-1,1])*self.beams_dir_2d

        # Start to play survey data. Necessary to keep the PF and auv_2_ros in synch
        synch_top = rospy.get_param("~synch_topic", '/pf_synch')
        self.synch_pub = rospy.Publisher(synch_top, Bool, queue_size=10)
        msg = Bool()
        msg.data = True

        # Transforms from auv_2_ros
        try:
            rospy.loginfo("Waiting for transforms")
            mbes_tf = tfBuffer.lookup_transform('hugin/base_link', 'hugin/mbes_link',
                                                rospy.Time(0), rospy.Duration(35))
            self.base2mbes_mat = matrix_from_tf(mbes_tf)

            m2o_tf = tfBuffer.lookup_transform(self.map_frame, odom_frame,
                                               rospy.Time(0), rospy.Duration(35))
            self.m2o_mat = matrix_from_tf(m2o_tf)

            rospy.loginfo("Transforms locked - pf node")
        except:
            rospy.loginfo("ERROR: Could not lookup transform from base_link to mbes_link")

        # Initialize list of particles
        self.particles = np.empty(self.pc, dtype=object)
        for i in range(self.pc):
            self.particles[i] = Particle(self.beams_num, self.pc, i, self.base2mbes_mat,
                                         self.m2o_mat, init_cov=init_cov, meas_std=meas_std,
                                         process_cov=motion_cov)
            self.particles[i].ID = self.p_ID
            self.p_ID += 1
        
        # PF filter created. Start auv_2_ros survey playing
        rospy.loginfo("Particle filter class successfully created")
        self.synch_pub.publish(msg)
        
        finished_top = rospy.get_param("~survey_finished_top", '/survey_finished')
        self.finished_sub = rospy.Subscriber(finished_top, Bool, self.synch_cb)
        self.survey_finished = False

        # Start timing now
        self.time = rospy.Time.now().to_sec()
        self.old_time = rospy.Time.now().to_sec()

        # Create particle to compute DR
        self.dr_particle = Particle(self.beams_num, self.pc, self.pc+1, self.base2mbes_mat,
                                    self.m2o_mat, init_cov=[0.]*6, meas_std=meas_std,
                                    process_cov=motion_cov)

        rospy.spin()



    def synch_cb(self, finished_msg):
        rospy.loginfo("PF node: Survey finished received") 
        # rospy.signal_shutdown("Survey finished")


    def gp_meas_model(self, real_mbes, p_part, r_base):
        # Nacho: the pings are already in the map frame
        # # Transform beams to particle mbes frame and compute map coordinates
        # real_mbes = np.dot(r_base, real_mbes.T)
        # real_mbes = np.add(real_mbes.T, p_part)

        # Sample GP here
        mu, sigma = self.gp.sample(np.asarray(real_mbes)[:, 0:2])
        mu_array = np.array([mu])
        # sigma_array = np.array([sigma])
        
        # Concatenate sampling points x,y with sampled z
        mbes_gp = np.concatenate((np.asarray(real_mbes)[:, 0:2], 
                                  mu_array.T), axis=1)
        return mbes_gp, sigma, mu

    def mbes_real_cb(self, msg):
        # Beams in vehicle mbes frame
        real_mbes_full = pcloud2ranges_full(msg)
        # Selecting only self.beams_num of beams in the ping
        idx = np.round(np.linspace(0, len(real_mbes_full)-1,
                                           self.beams_num)).astype(int)
        # Store in pings history
        self.mbes_history.append(real_mbes_full[idx])

        # When ping received, update particles trajectory histories
        for i in range(self.pc):
            self.particles[i].update_pose_history()
        
        # Store latest mbes msg for timing
        self.latest_mbes = msg

        self.pings_since_training += 1


    # Action server to simulate MBES for the sim AUV
    def mbes_as_cb(self, goal):

        # Unpack goal
        p_mbes = [goal.mbes_pose.transform.translation.x, 
                 goal.mbes_pose.transform.translation.y, 
                 goal.mbes_pose.transform.translation.z]
        r_mbes = quaternion_matrix([goal.mbes_pose.transform.rotation.x,
                                    goal.mbes_pose.transform.rotation.y,
                                    goal.mbes_pose.transform.rotation.z,
                                    goal.mbes_pose.transform.rotation.w])[0:3, 0:3]
                
        # IGL sim ping
        # The sensor frame on IGL needs to have the z axis pointing opposite from the actual sensor direction
        R_flip = rotation_matrix(np.pi, (1,0,0))[0:3, 0:3]
        mbes = self.draper.project_mbes(np.asarray(p_mbes), r_mbes,
                                        goal.beams_num.data, self.mbes_angle)
        
        mbes = mbes[::-1] # Reverse beams for same order as real pings
        
        # Transform points to MBES frame (same frame than real pings)
        rot_inv = r_mbes.transpose()
        p_inv = rot_inv.dot(p_mbes)
        mbes = np.dot(rot_inv, mbes.T)
        mbes = np.subtract(mbes.T, p_inv)

        # Pack result with ping in map frame
        mbes_cloud = pack_cloud(self.mbes_frame, mbes)
        result = MbesSimResult()
        result.sim_mbes = mbes_cloud
        self.as_ping.set_succeeded(result)

        # self.latest_mbes = result.sim_mbes 
        self.count_pings += 1
        for i in range(self.pc):
            self.particles[i].ctr += 1

    def odom_callback(self, odom_msg):
        self.time = odom_msg.header.stamp.to_sec()
        if self.old_time and self.time > self.old_time:
            # Motion prediction
            self.predict(odom_msg)    
            
            if self.latest_mbes.header.stamp > self.prev_mbes.header.stamp:    
                # Measurement update if new one received
                weights = self.update_maps(self.latest_mbes, odom_msg)
                self.prev_mbes = self.latest_mbes
                
                # Particle resampling
                self.resample(weights)
                self.update_rviz()
                self.publish_stats(odom_msg)

        self.old_time = self.time

    def predict(self, odom_t):
        dt = self.time - self.old_time
        for i in range(0, self.pc):
            self.particles[i].motion_pred(odom_t, dt)

        # Predict DR
        self.dr_particle.motion_pred(odom_t, dt)


    def cb_lengthscale(self, msg):
        arr = msg.data
        idx = int(arr[-1]) # Particle index
        if int(arr[0]) == 0:
            self.particles[idx].time4regression = True # if data = 0 can keep on training
        else:
            self.particles[idx].time4regression = False # if data = 1 stop training


    def gp_meanvar_cb(self, msg):
        arr = msg.data
        idx = int(arr[-1]) # Particle idx
        arr = np.delete(arr, -1)
        n = int(arr.shape[0] / 2)
        cloud = arr.reshape(n,2)
        mu_est = cloud[:,0]
        sigma_est = cloud[:,1]
        self.calculateLikelihood( mu_est, sigma_est, idx, logLkhood=True)


    def calculateLikelihood(self, mu_est, sigma_est, idx, logLkhood):        
        # collect data
        mu_obs = self.particles[idx].mu_list[0]
        sigma_obs = self.particles[idx].sigma_list[0]
        # pop the latesed used
        self.particles[idx].mu_list.pop(0)
        self.particles[idx].sigma_list.pop(0)
        try:
            # divide the equation into subtasks
            mu = np.asarray(mu_est) - np.asarray(mu_obs)
            sigma = np.asarray(sigma_est) + np.asarray(sigma_obs) # sigma_est^2 + sigma_obs ^2
            # convert sigma to a matrix
            sigma = np.diag(sigma)

            if logLkhood:
                self.dim = len(mu) # Dimension
                self.norm_const =  self.dim * np.log (2 * np.pi) 

                nom = np.dot(  np.dot( mu , np.linalg.inv(sigma) ) , np.transpose(mu))
                _, detSigma = np.linalg.slogdet(sigma)
                logdetSigma = np.log(detSigma)
                lkhood = -0.5 * (nom + self.norm_const + logdetSigma)
                wi = abs(1/lkhood) # since it's negative, the highest negative value should have the lowest weight (e^-inf = 0)
                # lkhood = np.exp(lkhood)

            else:
                self.dim = len(mu) # Dimension
                self.norm_const = np.sqrt( np.power( mpf(2 * np.pi) , self.dim)) # mpf can handle large float numbers
                nom = -0.5 * np.dot(  np.dot( mu , np.linalg.inv(sigma) ) , np.transpose(mu)) # -1/2 * mu^T * Sigma^-1 * mu
                _, detSigma = np.linalg.slogdet(sigma)
                denom = self.norm_const * np.sqrt(detSigma)
                # denom = np.sqrt( np.power( 2 * np.pi, dim) * np.linalg.det(sigma)) # sgrt( (2pi)^dim * Det(sigma))
                
                # calculate the likelihood
                lkhood = np.exp (nom) / denom
                wi = lkhood

        except ValueError:
            print('Likelihood = 0.0')
            print('sigma obs ', sigma_obs.shape)
            print('sigma est ', sigma_est.shape)
            print('mu obs ', mu_obs.shape)
            print('mu est ', mu_est.shape)
            wi = 0.0
        # convert likelihood into weigh
        self.particles[idx].w = wi 


    def update_maps(self, real_mbes, odom):
             
        # To transform from base to mbes
        R = self.base2mbes_mat.transpose()[0:3,0:3]
        
        # If time to retrain GP map
        if self.pings_since_training > 100:
            for i in range(0, self.pc):           
                # Transform each MBES ping in vehicle frame to the particle trajectory 
                # (result in map frame)
                part_ping_map = []
                for j in range(0, len(self.mbes_history)): 
                    # For particle i, get all its trajectory in the map frame
                    p_part, r_mbes = self.particles[i].pose_history[j]
                    r_base = r_mbes.dot(R) # The GP sampling uses the base_link orientation 

                    part_i_ping_map = np.dot(r_base, self.mbes_history[j].T)
                    part_ping_map.append(np.add(part_i_ping_map.T, p_part)) 
                # As array
                pings_i = np.asarray(part_ping_map)
                pings_i = np.reshape(pings_i, (-1,3))            
                    
                # Publish (for visualization)
                mbes_pcloud = pack_cloud(self.map_frame, pings_i)
                self.pcloud_pub.publish(mbes_pcloud)

                # Retrain the particle's GP
                print("Training GP ", i)
                self.particles[i].gp.fit(pings_i[:,0:2], pings_i[:,2], n_samples= 200, 
                                         max_iter=100, learning_rate=1e-1, rtol=1e-4, 
                                         ntol=100, auto=False, verbose=False)
                # Plot posterior
                self.particles[i].gp.plot(pings_i[:,0:2], pings_i[:,2], 
                                          self.storage_path + 'gp_result/' + 'particle_' + str(i) 
                                          + '_training_' + str(self.count_training) + '.png',
                                          n=100, n_contours=100 )
                
                # self.particles[i].pings.clear()
                print("GP trained ", i)

                # # If the latest ping in this particle frame is close enough to the particle's GP map --> potential LC
                
                # # Calculate expected meas from the particle GP
                # exp_ping_map, exp_sigs, mu_obs = self.gp_meas_model(real_mbes_full, p_part, r_base)
                # self.particles[i].meas_cov = np.diag(exp_sigs)

                # # Update particle's weight comparing GP expected meas and real ping from AUV
                # self.particles[i].compute_weight(exp_ping_map, part_ping_map)

                # ----------- record input data --------
                # self.particles[i].inputs = np.append(self.particles[i].inputs, exp_mbes[:,:2], axis=0)  # (n,2)
                # self.particles[i].est_map = np.append(self.particles[i].est_map, exp_mbes, axis=0)  # (n,3)
                # self.particles[i].sigma_obs = np.append(self.particles[i].sigma_obs, exp_sigs, axis=0)
                # self.particles[i].mu_obs = np.append(self.particles[i].mu_obs, exp_mbes[:,2], axis=0)#exp_mbes[:,2])# mu_obs)
                # trajectory = np.array([[self.particles[i].p_pose[0], self.particles[i].p_pose[1], self.particles[i].p_pose[2], self.particles[i].p_pose[3], self.particles[i].p_pose[4], self.particles[i].p_pose[5] ]])
                # self.particles[i].trajectory_path = np.append(self.particles[i].trajectory_path, trajectory, axis=0) #save the trajectory for later use
                
                # # record_data seems to be a var to start to collect data for the GPs when starting the algorithm
                # if self.particles[i].ctr > self.record_data:
                #     old_mbes_x = self.particles[i].est_map[0:self.beams_num, 0] #est_map[self.particles[i].n_from : self.particles[i].n_from + self.beams_num, 0]
                #     old_mbes_y = self.particles[i].est_map[0:self.beams_num, 1] #est_map[self.particles[i].n_from : self.particles[i].n_from + self.beams_num, 1]
                #     dist = np.sqrt( (exp_mbes[:,0] - old_mbes_x)**2 + (exp_mbes[:,1] - old_mbes_y)**2)
                #     dist_accepted = sum(dist < self.th_reg)

                #     # time4regression = checks the lengthscale of the particle GP to stop training
                #     if self.particles[i].time4regression and dist_accepted >= self.beams_num * self.gamma:   # when to re-calculate the svgp 
                #         self.ctr += 1
                #         self.particles[i].ctr = 0
                #         self.particles[i].n_from = len(self.particles[i].mu_obs) - self.beams_num
                #         self.regression(i, 0) # sending data to re-calculate svgp
                    # ctr = 0
                    # N_beams = len(self.targets)
                    # B_min = N_beams * self.gamma

            # Reset pings for training
            self.count_training += 1
            self.pings_since_training = 0

        weights = []
        for i in range(self.pc):
            weights.append(self.particles[i].w) 
        # Number of particles that missed some beams 
        # (if too many it would mess up the resampling)
        self.miss_meas = weights.count(0.0)
        weights_array = np.asarray(weights)
        # Add small non-zero value to avoid hitting zero
        weights_array += 1.e-200
        return weights_array


    def regression(self, i, final):
        inputs = np.zeros((1,2))
        targets = np.zeros((1,))
        mu = np.zeros((1,))
        sigma = np.zeros((1,))
        p = self.particles[i]
        p.targets = self.targets

        while True: # sending both the particles data plus the ancestors
            inputs = np.append(inputs, p.inputs, axis=0)
            targets = np.append(targets, p.targets, axis=0)
            mu = np.append(mu, p.mu_obs, axis=0)
            sigma = np.append(sigma, p.sigma_obs, axis=0)

            if p.parent == None:
                break
            for leaf in self.tree_list:
                if p.parent == leaf.ID:
                    p = leaf
                    break
        # this if-statment is for the final trajctory, sometimes when the script keeps running its collecting more data and I need len(target) = len(input)
        if len(targets) == inputs.shape[0]:
            cloud_arr = np.zeros((len(targets)-2,3))
        elif len(targets) >= inputs.shape[0]:
            targets = targets[:-self.beams_num]
            cloud_arr = np.zeros((len(targets)-2,3))
        elif len(targets) <= inputs.shape[0]:
            inputs = inputs[:-self.beams_num,:]
            cloud_arr = np.zeros((len(targets)-2,3))

        cloud_arr[:,:2] = inputs[2:,:]
        cloud_arr[:,2] = targets[2:]
        cloud_arr = cloud_arr.reshape(cloud_arr.shape[0]*cloud_arr.shape[1], 1)
        cloud_arr = np.append(cloud_arr, i) # insert particle index
        cloud_arr = np.append(cloud_arr, final) # insert if final or not
        msg = Floats()
        msg.data = cloud_arr
        self.gp_pub.publish(msg) # training particles svgp in: train_pf_gp.py
        # save observation data to calculate likelihood later 
        self.particles[i].mu_list.append(mu[2:])
        self.particles[i].sigma_list.append(sigma[2:])


    def publish_stats(self, gt_odom):
        # Send statistics for visualization
        p_odom = self.dr_particle.p_pose
        stats = np.array([self.n_eff_filt,
                          self.pc/2.,
                          gt_odom.pose.pose.position.x,
                          gt_odom.pose.pose.position.y,
                          gt_odom.pose.pose.position.z,
                          self.avg_pose.pose.pose.position.x,
                          self.avg_pose.pose.pose.position.y,
                          self.avg_pose.pose.pose.position.z,
                          p_odom[0],
                          p_odom[1],
                          p_odom[2],
                          self.cov[0,0],
                          self.cov[0,1],
                          self.cov[0,2],
                          self.cov[1,1],
                          self.cov[1,2],
                         self.cov[2,2]], dtype=np.float32)

        self.stats.publish(stats) 


    def ping2ranges(self, point_cloud):
        ranges = []
        cnt = 0
        for p in pc2.read_points(point_cloud, 
                                 field_names = ("x", "y", "z"), skip_nans=True):
            ranges.append(np.linalg.norm(p[-2:]))
        
        return np.asarray(ranges)
    
    def moving_average(self, a, n=3) :
        ret = np.cumsum(a, dtype=float)
        ret[n:] = ret[n:] - ret[:-n]
        return ret[n - 1:] / n

    def create_dir(self, name): 
        if not os.path.exists(self.storage_path + name):
            os.makedirs(self.storage_path + name)

    def resample(self, weights):
        # Normalize weights
        weights /= weights.sum()
        N_eff = self.pc
        if weights.sum() == 0.:
            rospy.loginfo("All weights zero!")
        else:
            N_eff = 1/np.sum(np.square(weights))

        self.n_eff_mask.pop(0)
        self.n_eff_mask.append(N_eff)
        self.n_eff_filt = self.moving_average(self.n_eff_mask, 3) 
        # print ("N_eff ", N_eff)
        # print('n_eff_filt ', self.n_eff_filt)
        # print ("Missed meas ", self.miss_meas)
                
        # Resampling?
        if self.n_eff_filt < self.pc/2. and self.miss_meas <= self.pc/2.:
            self.time2resample = False
            self.ctr = 0
            rospy.loginfo('resampling')
            print('n_eff_filt ', self.n_eff_filt)
            print ("Missed meas ", self.miss_meas)
            indices = residual_resample(weights)
            keep = list(set(indices))
            lost = [i for i in range(self.pc) if i not in keep]
            dupes = indices[:].tolist()
            for i in keep:
                dupes.remove(i)

            self.ancestry_tree(lost, dupes) # save parent and children
            self.reassign_poses(lost, dupes)
            
            # Add noise to particles
            for i in range(self.pc):
                self.particles[i].add_noise(self.res_noise_cov)
                # clear data
                self.particles[i].trajectory_path = np.zeros((1,6))
                self.particles[i].est_map = np.zeros((1,3))
                self.particles[i].inputs = np.zeros((1,2))
                self.particles[i].mu_obs = np.zeros((1,))
                self.particles[i].sigma_obs = np.zeros((1,))
                self.particles[i].n_from = 1
            self.observations = np.zeros((1,3))
            self.mapping = np.zeros((1,3))
            self.targets = np.zeros((1,))
            self.n_from = 1


    def ancestry_tree(self, lost, dupes):

        if self.one_time: # for the first particles, created at start
            self.one_time = False
            for i in range(self.pc):
                particle_tree = atree(self.particles[i].ID, None, self.particles[i].trajectory_path[1:,:], self.observations[1:,:] )
                particle_tree.mapping = self.particles[i].est_map[1:,:]
                particle_tree.inputs = self.particles[i].inputs[1:,:]
                particle_tree.targets = self.targets[1:]
                particle_tree.mu_obs = self.particles[i].mu_obs[1:]
                particle_tree.sigma_obs = self.particles[i].sigma_obs[1:]
                self.tree_list.append(particle_tree) # ID = index

        for i in range(len(lost)):
            self.particles[lost[i]].ID = self.p_ID
            self.particles[lost[i]].parent = self.particles[dupes[i]].ID
            idx_child = self.particles[lost[i]].ID
            idx_parent = self.particles[dupes[i]].ID
            particle_tree = atree(idx_child, idx_parent, self.particles[dupes[i]].trajectory_path[1:,:], self.observations[1:,:] )
            particle_tree.mapping = self.particles[dupes[i]].est_map[1:,:]
            particle_tree.inputs = self.particles[lost[i]].inputs[1:,:]
            particle_tree.targets = self.targets[1:]
            particle_tree.mu_obs = self.particles[lost[i]].mu_obs[1:]
            particle_tree.sigma_obs = self.particles[lost[i]].sigma_obs[1:]
            self.tree_list.append(particle_tree)
            self.tree_list[idx_parent].children.append(idx_child)
            self.p_ID += 1 # all particles have their own unique ID

        set_dupes = set(dupes)
        unique_parent = (list(set_dupes))
        # merge parent and child if only one child
        for i in range(self.pc):
            if i not in lost and i not in unique_parent: 
                unique_parent.append(i)
        # Now save the old parent as its child
        for i in range(len(unique_parent)):
            idx_parent = self.particles[unique_parent[i]].ID
            idx_child = self.p_ID
            self.particles[unique_parent[i]].parent = self.particles[unique_parent[i]].ID
            self.particles[unique_parent[i]].ID = idx_child

            particle_tree = atree(idx_child, idx_parent, self.particles[unique_parent[i]].trajectory_path[1:,:], self.observations[1:,:] )
            particle_tree.mapping = self.particles[unique_parent[i]].est_map[1:,:]
            particle_tree.inputs = self.particles[unique_parent[i]].inputs[1:,:]
            particle_tree.targets = self.targets[1:]
            particle_tree.mu_obs = self.particles[unique_parent[i]].mu_obs[1:]
            particle_tree.sigma_obs = self.particles[unique_parent[i]].sigma_obs[1:]
            self.tree_list.append(particle_tree)
            self.tree_list[idx_parent].children.append(idx_child)
            self.p_ID += 1 # all particles have their own unique ID


    def save_final_gp_cb(self, msg): #when you select a number in: Save_trajectory.py
        rospy.loginfo('... saving final gp map\n')
        idx = int(msg.data)
        print('final particle ', idx)
        self.regression(idx, 99)


    def save_trajectory_cb(self, msg): #when you press 't' in: Save_trajectory.py
        rospy.loginfo('... saving trajectory\n')
        tr_path = 'trajectory/'
        self.create_dir(tr_path)

        for i in range(self.pc):
            particle_tree = self.particles[i]
            particle_tree.trajectory = self.particles[i].trajectory_path[1:,:]
            particle_tree.observations = self.observations[1:,:]
            particle_tree.mapping = self.particles[i].est_map[1:,:]
            particle_tree.targets = self.targets[1:]
            f_name = tr_path + 'p' + str(i) + '/'
            # creating dir if missing
            self.create_dir(f_name)
            self.create_dir(f_name + 'localization/')
            self.create_dir(f_name + 'mapping/')
            self.create_dir(f_name + 'localization/tr_path/')
            self.create_dir(f_name + 'localization/obs_path/')
            self.create_dir(f_name + 'mapping/est_map/')
            self.create_dir(f_name + 'mapping/obs_depth/')
            while True:
                np.save(self.storage_path + f_name +'localization/tr_path/' + 'ID' + str(particle_tree.ID) + 'tr.npy', particle_tree.trajectory)
                np.save(self.storage_path + f_name + 'localization/obs_path/'+ 'ID' + str(particle_tree.ID) + 'obs.npy', particle_tree.observations)
                np.save(self.storage_path + f_name +'mapping/est_map/' + 'ID' + str(particle_tree.ID) + 'map.npy', particle_tree.mapping)
                np.save(self.storage_path + f_name +'mapping/obs_depth/' + 'ID' + str(particle_tree.ID) + 'map.npy', particle_tree.targets)
                if particle_tree.parent == None:
                    break
                for leaf in self.tree_list:
                    if particle_tree.parent == leaf.ID:
                        particle_tree = leaf
                        break
        
        rospy.loginfo('Trajectory saved.')


    def reassign_poses(self, lost, dupes):
        for i in range(len(lost)):
            # Faster to do separately than using deepcopy()
            self.particles[lost[i]].p_pose = self.particles[dupes[i]].p_pose
    
    def average_pose(self, pose_list):
        poses_array = np.array(pose_list)
        ave_pose = poses_array.mean(axis = 0)
        self.avg_pose.pose.pose.position.x = ave_pose[0]
        self.avg_pose.pose.pose.position.y = ave_pose[1]
        self.avg_pose.pose.pose.position.z = ave_pose[2]
        roll  = ave_pose[3]
        pitch = ave_pose[4]

        # Wrap up yaw between -pi and pi        
        poses_array[:,5] = [(yaw + np.pi) % (2 * np.pi) - np.pi 
                             for yaw in  poses_array[:,5]]
        yaw = np.mean(poses_array[:,5])
        
        self.avg_pose.pose.pose.orientation = Quaternion(*quaternion_from_euler(roll,
                                                                                pitch,
                                                                                yaw))
        self.avg_pose.header.stamp = rospy.Time.now()
        self.avg_pub.publish(self.avg_pose)
        
        # Calculate covariance
        self.cov = np.zeros((3, 3))
        for i in range(self.pc):
            dx = (poses_array[i, 0:3] - ave_pose[0:3])
            self.cov += np.diag(dx*dx.T) 
            self.cov[0,1] += dx[0]*dx[1] 
            self.cov[0,2] += dx[0]*dx[2] 
            self.cov[1,2] += dx[1]*dx[2] 
        self.cov /= self.pc

        # TODO: exp meas from average pose of the PF, for change detection


    # TODO: publish markers instead of poses
    #       Optimize this function
    def update_rviz(self):
        self.poses.poses = []
        pose_list = []
        for i in range(self.pc):
            pose_i = Pose()
            pose_i.position.x = self.particles[i].p_pose[0]
            pose_i.position.y = self.particles[i].p_pose[1]
            pose_i.position.z = self.particles[i].p_pose[2]
            pose_i.orientation = Quaternion(*quaternion_from_euler(
                self.particles[i].p_pose[3],
                self.particles[i].p_pose[4],
                self.particles[i].p_pose[5]))

            self.poses.poses.append(pose_i)
            pose_list.append(self.particles[i].p_pose)
        
        # Publish particles with time odometry was received
        self.poses.header.stamp = rospy.Time.now()
        self.pf_pub.publish(self.poses)
        self.average_pose(pose_list)




if __name__ == '__main__':

    rospy.init_node('rbpf_slam_node', disable_signals=False)
    try:
        rbpf_slam()
    except rospy.ROSInterruptException:
        rospy.logerr("Couldn't launch rbpf_node")
        pass
