#include "rbpf_particle.h"

RbpfParticle::RbpfParticle(int beams_num, int p_num, int index, Eigen::Matrix4f mbes_tf_matrix,
                           Eigen::Matrix4f m2o_matrix, std::vector<float> init_cov, float meas_std,
                           std::vector<float> process_cov)
{
    p_num_ = p_num;
    index_ = index;
    beams_num_ = beams_num;
    p_pose_ = Eigen::VectorXf::Zero(6,1);
    mbes_tf_matrix_ = mbes_tf_matrix;
    m2o_matrix_ = m2o_matrix;

    // Noise models init
    init_cov_ = init_cov;
    process_cov_ = process_cov;
    meas_cov_ = std::vector<float>(beams_num_, std::pow(meas_std, 2));

    this->add_noise(init_cov_);

}

RbpfParticle::~RbpfParticle()
{
    
}

void RbpfParticle::add_noise(std::vector<float> &noise){

    // Generate noise
    // Eigen::VectorXf noisy_pose(6, 1);
    std::random_device rd{};
    std::mt19937 seed{rd()};
    for (int i = 0; i < 6; i++)
    {
        std::normal_distribution<float> sampler{0, std::sqrt(noise.at(i))};
        p_pose_(i) += sampler(seed);
    }
}

void RbpfParticle::motion_prediction(nav_msgs::Odometry &odom_t, float dt){

    // Generate noise
    std::random_device rd{};
    std::mt19937 seed{rd()};
    Eigen::VectorXf noise_vec(6, 1);
    for (int i = 0; i < 6; i++)
    {
        std::normal_distribution<float> sampler{0, std::sqrt(process_cov_.at(i))};
        noise_vec(i) = sampler(seed);
    }

    // Angular 
    Eigen::Vector3f vel_rot = Eigen::Vector3f(odom_t.twist.twist.angular.x,
                                              odom_t.twist.twist.angular.y,
                                              odom_t.twist.twist.angular.z);

    Eigen::Vector3f rot_t = p_pose_.tail(3) + vel_rot * dt + noise_vec.tail(3);
    // Wrap up angles
    for (int i =0; i < 3; i++){
        rot_t(i) = angle_limit(rot_t(i));
    }
    p_pose_.tail(3) = rot_t;

    Eigen::AngleAxisf rollAngle(rot_t(0), Eigen::Vector3f::UnitX());
    Eigen::AngleAxisf pitchAngle(rot_t(1), Eigen::Vector3f::UnitY());
    Eigen::AngleAxisf yawAngle(rot_t(2), Eigen::Vector3f::UnitZ());
    Eigen::Quaternion<float> q = rollAngle * pitchAngle * yawAngle;
    Eigen::Matrix3f rotMat = q.matrix();

    // Linear
    Eigen::Vector3f vel_p = Eigen::Vector3f(odom_t.twist.twist.linear.x,
                                            odom_t.twist.twist.linear.y,
                                            odom_t.twist.twist.linear.z);
    
    Eigen::Vector3f step_t = rotMat * (vel_p * dt) + noise_vec.head(3);
    p_pose_.head(3) += step_t;
}


void RbpfParticle::update_pose_history()
{
    // Rotation matrix
    Eigen::AngleAxisf rollAngle(p_pose_(3), Eigen::Vector3f::UnitX());
    Eigen::AngleAxisf pitchAngle(p_pose_(4), Eigen::Vector3f::UnitY());
    Eigen::AngleAxisf yawAngle(p_pose_(5), Eigen::Vector3f::UnitZ());
    Eigen::Quaternion<float> q = rollAngle * pitchAngle * yawAngle;
    Eigen::Matrix3f rotMat = q.matrix();

    // Particle pose in homogenous coordinates
    Eigen::Matrix4f t_p = Eigen::Matrix4f::Identity();
    t_p.topLeftCorner(3, 3) = rotMat;
    t_p.block(0,3,3,1) = p_pose_.head(3);

    // std::cout << "------" << std::endl;
    // std::cout << t_p << std::endl;
    // std::cout << m2o_matrix_ << std::endl;
    // std::cout << mbes_tf_matrix_ << std::endl;

    Eigen::Matrix4f p_pose_map = m2o_matrix_ * t_p * mbes_tf_matrix_;

    // std::cout << p_pose_map.block(0, 3, 3, 1).matrix() << std::endl;
    // std::cout << p_pose_map.topLeftCorner(3, 3).matrix() << std::endl;
    // std::cout << "------" << std::endl;

    pos_history_.push_back(p_pose_map.block(0, 3, 3, 1));
    rot_history_.push_back(p_pose_map.topLeftCorner(3,3));

}

void RbpfParticle::get_p_mbes_pose()
{
    // Nacho: this functionality has been moved to update_pose_history()
    // This method is left to keep parallelism with the original python class
}

float angle_limit(float angle) // keep angle within [0;2*pi]
{
    while (angle >= M_PI*2)
        angle -= M_PI*2;
    while (angle < 0)
        angle += M_PI*2;
    return angle;
}

sensor_msgs::PointCloud2 pack_cloud(string frame, std::vector<Eigen::RowVector3f> mbes)
{
    sensor_msgs::PointCloud2 mbes_pcloud; 
    PCloud::Ptr pcl_pcloud(new PCloud);
    pcl_pcloud->header.frame_id = frame;
    // pcl_pcloud->height = pcl_pcloud->width = 1;
    for(int i = 0; i < mbes.size(); i++)
    {
        pcl_pcloud->points.push_back(pcl::PointXYZ(mbes[i][0], mbes[i][1], mbes[i][2]));
    }

    pcl::toROSMsg(*pcl_pcloud.get(), mbes_pcloud);
    
    return mbes_pcloud;
}

Eigen::ArrayXXf pcloud2ranges_full(const sensor_msgs::PointCloud2& point_cloud, int beams_num)
{
    // Nacho: is this needed?
    sensor_msgs::PointCloud out_cloud;
    sensor_msgs::convertPointCloud2ToPointCloud(point_cloud, out_cloud);

    // Selecting only self.beams_num of beams in the ping
    std::vector<int> idx = linspace(0, point_cloud.width - 1, beams_num);
    Eigen::MatrixXf beams(beams_num, 3);
    for (int i = 0; i < out_cloud.points.size(); ++i)
    {
        if (std::find(idx.begin(), idx.end(), i) != idx.end() ){
            beams.row(i) = Eigen::Vector3f(out_cloud.points[i].x, 
                                            out_cloud.points[i].y,
                                            out_cloud.points[i].z);
        }
    }
    return beams;
}

// A function to generate numpy linspace 
std::vector<int> linspace(float start, float end, float num)
{
    std::vector<int> linspaced;

    if (0 != num)
    {
        if (1 == num) 
        {
            linspaced.push_back(static_cast<int>(start));
        }
        else
        {
            float delta = (end - start) / (num - 1);

            for (auto i = 0; i < (num - 1); ++i)
            {
                linspaced.push_back(static_cast<int>(start + delta * i));
            }
            // ensure that start and end are exactly the same as the input
            linspaced.push_back(static_cast<int>(end));
        }
    }
    return linspaced;
}