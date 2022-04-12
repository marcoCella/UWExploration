#pragma once

#include <math.h>
#include <ros/ros.h>

#include <Eigen/Dense>
#include <Eigen/Core>
#include <eigen_conversions/eigen_msg.h>
#include <tf_conversions/tf_eigen.h>

#include <std_msgs/Header.h>

#include <sensor_msgs/PointCloud.h>
#include <sensor_msgs/PointCloud2.h>
#include <sensor_msgs/point_cloud_conversion.h>
#include <sensor_msgs/PointField.h>
#include <nav_msgs/Odometry.h>

#include <pcl_ros/point_cloud.h>
#include <pcl/point_types.h>
#include <pcl_conversions/pcl_conversions.h>
#include <pcl_ros/transforms.h>

#include <geometry_msgs/Point32.h>

#include <random>

using namespace std;

typedef pcl::PointCloud<pcl::PointXYZ> PCloud;

class RbpfParticle
{

public:
    RbpfParticle(int beams_num, int pc, int i, Eigen::Matrix4f base2mbes_mat,
                 Eigen::Matrix4f m2o_matrix, std::vector<float> init_cov, float meas_std,
                 std::vector<float> process_cov);
    ~RbpfParticle();
    
    void add_noise(std::vector<float> &noise);

    void motion_prediction(nav_msgs::Odometry &odom_t, float dt);

    void compute_weight(Eigen::VectorXd exp_mbes, Eigen::VectorXd real_mbes);

    float weight_mv(Eigen::VectorXd &mbes_meas_ranges, Eigen::VectorXd &mbes_sim_ranges);

    void update_pose_history();

    void get_p_mbes_pose();

    Eigen::VectorXf p_pose_;
    std::vector<Eigen::Vector3f, Eigen::aligned_allocator<Eigen::Vector3f>> pos_history_;
    std::vector<Eigen::Matrix3f, Eigen::aligned_allocator<Eigen::Matrix3f>> rot_history_;

private:
    float w_;
    float log_w;

    // For gp and likelihood
    Eigen::ArrayXf inputs_;
    Eigen::ArrayXf est_map_;
    Eigen::ArrayXf sigma_obs_;
    vector<float> sigma_list_;
    vector<float> mu_list_;

    // For the ancestry tree
    Eigen::ArrayXf trajectory_path_;
    bool time4regression_;
    string parent_;
    int n_from_;
    int ctr_;

    // Nacho
    vector<tuple<Eigen::ArrayXf, Eigen::ArrayXXf>> pose_history_;

    // Particle
    int beams_num_; 
    int p_num_;
    int index_; 

    Eigen::Matrix4f mbes_tf_matrix_;
    Eigen::Matrix4f m2o_matrix_;

    // Noise models
    std::vector<float> init_cov_;
    std::vector<double> meas_cov_;  //TODO: implement this one on the weights?
    std::vector<float> process_cov_;

};

float angle_limit(float angle);

sensor_msgs::PointCloud2 pack_cloud(string frame, std::vector<Eigen::RowVector3f> mbes);

Eigen::ArrayXXf pcloud2ranges_full(const sensor_msgs::PointCloud2& point_cloud, int beams_num);

vector<float> list2ranges(vector<Eigen::Array3f> points);

std::vector<int> linspace(float start, float end, float num);

float mvn_pdf(const Eigen::VectorXd& x, Eigen::VectorXd& mean, Eigen::MatrixXd& sigma);

struct normal_random_variable_
{

    normal_random_variable_(Eigen::VectorXd const &mean, Eigen::MatrixXd const &covar)
        : mean(mean)
    {
        Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigenSolver(covar);
        transform = eigenSolver.eigenvectors() * eigenSolver.eigenvalues().cwiseSqrt().asDiagonal();
    }

    Eigen::VectorXd mean;
    Eigen::MatrixXd transform;

    Eigen::VectorXd operator()() const
    {
        static std::mt19937 gen{std::random_device{}()};
        static std::normal_distribution<> dist;

        return mean + transform * Eigen::VectorXd{mean.size()}.unaryExpr([&](auto x) { return dist(gen); });
    }
};
