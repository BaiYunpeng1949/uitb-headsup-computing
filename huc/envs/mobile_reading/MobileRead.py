import numpy as np
from collections import Counter
import mujoco
import os

from gym import Env
from gym.spaces import Box, Dict

import yaml
from scipy.ndimage import gaussian_filter

from huc.utils.rendering import Camera, Context


class Read(Env):

    def __init__(self):
        """ Model the reading under stationary and mobile (cause perturbations) conditions """
        # Get directory of this file
        directory = os.path.dirname(os.path.realpath(__file__))

        # Read the configurations from the YAML file.
        root_dir = os.path.dirname(os.path.dirname(directory))
        with open(os.path.join(root_dir, "config.yaml")) as f:
            self._config = yaml.load(f, Loader=yaml.FullLoader)

        # Load the MuJoCo model
        self._model = mujoco.MjModel.from_xml_path(os.path.join(directory, "mobile-read-v1.xml"))
        self._data = mujoco.MjData(self._model)
        mujoco.mj_forward(self._model, self._data)

        # Define how often policy is queried
        self._action_sample_freq = 20
        self._frame_skip = int((1 / self._action_sample_freq) / self._model.opt.timestep)

        # Get the primitive idx in MuJoCo
        self._eye_joint_x_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_JOINT, "eye-joint-x")
        self._eye_joint_y_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_JOINT, "eye-joint-y")
        self._perturbation_joint_z_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_JOINT, "perturbation-joint-z")
        self._perturbation_joint_x_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_JOINT,
                                                             "perturbation-joint-x")
        self._perturbation_joint_y_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_JOINT, "perturbation-joint-y")
        self._eye_body_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_BODY, "eye")
        self._sgp_ils100_body_mjidx = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_BODY,
                                                      "smart-glass-pane-interline-spacing-100")

        # Get targets (geoms that belong to "smart-glass-pane-interline-spacing-100")
        self._ils100_cells_mjidxs = np.where(self._model.geom_bodyid == self._sgp_ils100_body_mjidx)[0]

        self._sampled_target_mjidx = None

        # Define the target idx probability distribution
        self._VISUALIZE_RGBA = [1, 1, 0, 1]
        self._DFLT_RGBA = [0, 0, 0, 1]

        self._dwell_steps = int(1 * self._action_sample_freq)  # 1 seconds

        # Initialize task related parameters
        self._MODES = ["stationary", "mobile"]
        self._mode = None
        # Initialize the perturbation parameters
        # The exact amplitude is difficult to specify without direct measurement,
        # but some studies suggest a range of 2-5 cm for normal walking.
        # This is also dependent on a person's height, stride length, and other factors.
        self._perturbation_peak_z = 0.005       # Translation amplitude in z direction
        self._perturbation_peak_x = 0.001      # Translation amplitude in x direction
        self._perturbation_peak_y = 0.0125      # torsion amplitude in y direction
        gait_freq = 2     # 2 Hz
        self._perturbation_period_z = int(self._action_sample_freq / gait_freq)   # 0.5 seconds for normal human gait cycle modeled as a sine wave - 2 feet
        self._perturbation_period_x = int(self._action_sample_freq / gait_freq)  # 0.5 seconds for normal human gait cycle modeled as a sine wave
        self._perturbation_period_y = int(self._action_sample_freq / gait_freq)  # 0.5 seconds for normal human gait cycle modeled as a sine wave
        self._perturbation_velocity = None
        self._perturbation_amplitude = None

        # Oculomotor control related parameters
        # Start from action noises - the integration of oculomotor noise and the drifts after fixations.
        # The oculomotor noise is formalized as SDN, the zero-mean Gaussian noise with a standard deviation of
        # the signal proportional to the magnitude of the signal itself.
        # Now the saccade is a ballistic movement and can be finished in 1 action taken step (50 ms)
        self._rho_ocular_motor = 0.08   # The proportionality constant from paper: An Adaptive Model of Gaze-based Selection

        self._fixate_on_target = None

        # The fatigue related parameters
        self._last_step_saccade_qpos = None
        self._ctrl_list = None

        # Initialise RL related thresholds and counters
        self._steps = None
        self._on_target_steps = None
        self._num_trials = None  # Cells are already been read
        self._max_trials = 5  # Maximum number of cells to read - more trials in one episode will boost the convergence
        if self._config["rl"]["mode"] == "test":
            self._max_trials = len(self._ils100_cells_mjidxs)
        self.ep_len = int(self._max_trials * self._dwell_steps * 5)

        # Define the observation space
        width, height = 80, 80
        self._num_stk_frm = 1
        self._num_stateful_info = 6
        self.observation_space = Dict({
            "vision": Box(low=-1, high=1, shape=(self._num_stk_frm, width, height)),
            "proprioception": Box(low=-1, high=1, shape=(self._num_stk_frm * self._model.nq + self._model.nu,)),
            "stateful information": Box(low=-1, high=1, shape=(self._num_stateful_info,)),
        })

        # Define the action space
        self.action_space = Box(low=-1, high=1, shape=(2,))   # 2 dof eyeball rotation

        # Initialize the context and camera
        context = Context(self._model, max_resolution=[1280, 960])
        self._eye_cam = Camera(context, self._model, self._data, camera_id="eye", resolution=[width, height],
                               maxgeom=100,
                               dt=1 / self._action_sample_freq)
        self._env_cam = Camera(context, self._model, self._data, camera_id="env", maxgeom=100,
                               dt=1 / self._action_sample_freq)
        self._eye_cam_fovy = self._model.cam_fovy[mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_CAMERA, "eye")]

    @staticmethod
    def normalise(x, x_min, x_max, a, b):
        # Normalise x (which is assumed to be in range [x_min, x_max]) to range [a, b]
        return (b - a) * ((x - x_min) / (x_max - x_min)) + a

    def render(self, mode="rgb_array"):
        rgb, _ = self._env_cam.render()
        rgb_eye, _ = self._eye_cam.render()
        return rgb, rgb_eye

    def _get_obs(self):
        """ Get the observation of the environment """
        # Get the vision observation
        # Render the image
        rgb, _ = self._eye_cam.render()

        # Preprocess - H*W*C -> C*W*H
        rgb = np.transpose(rgb, [2, 1, 0])
        rgb_normalize = self.normalise(rgb, 0, 255, -1, 1)

        # Convert the rgb to grayscale - boost the training speed
        gray_normalize = rgb_normalize[0:1, :, :]*0.299 + rgb_normalize[1:2, :, :]*0.587 + rgb_normalize[2:3, :, :]*0.114
        gray_normalize = np.squeeze(gray_normalize, axis=0)
        vision = gray_normalize.reshape((-1, gray_normalize.shape[-2], gray_normalize.shape[-1]))

        # Get the proprioception observation
        proprioception = np.concatenate([self._data.qpos, self._data.ctrl])

        # Get the stateful information observation - normalize to [-1, 1]
        remaining_ep_len_norm = (self.ep_len - self._steps) / self.ep_len * 2 - 1
        remaining_dwell_steps_norm = (self._dwell_steps - self._on_target_steps) / self._dwell_steps * 2 - 1
        remaining_trials_norm = (self._max_trials - self._num_trials) / self._max_trials * 2 - 1
        sampled_target_idx_norm = self.normalise(self._sampled_target_mjidx, self._ils100_cells_mjidxs[0],
                                                 self._ils100_cells_mjidxs[-1], -1, 1)
        mode_norm = -1 if self._mode == self._MODES[0] else 1
        fixation_norm = 1 if self._fixate_on_target else -1
        stateful_info = np.array(
            [remaining_ep_len_norm, remaining_dwell_steps_norm, remaining_trials_norm, sampled_target_idx_norm,
             mode_norm, fixation_norm]
        )

        if stateful_info.shape[0] != self._num_stateful_info:
            raise ValueError("The shape of stateful information is not correct!")

        return {"vision": vision, "proprioception": proprioception, "stateful information": stateful_info}

    def reset(self):

        # Reset MuJoCo sim
        mujoco.mj_resetData(self._model, self._data)

        # Reset the variables and counters
        self._steps = 0
        self._on_target_steps = 0
        self._num_trials = 0

        self._ctrl_list = []
        self._last_step_saccade_qpos = np.array([0, 0])

        self._fixate_on_target = False

        # Initialize eyeball rotation angles
        self._data.qpos[self._eye_joint_x_mjidx] = np.random.uniform(-0.5, 0.5)
        self._data.qpos[self._eye_joint_y_mjidx] = np.random.uniform(-0.5, 0.5)

        self._mode = np.random.choice(self._MODES)
        if self._config["rl"]["mode"] == "debug" or self._config["rl"]["mode"] == "test":
            self._data.qpos[self._eye_joint_x_mjidx] = 0
            self._data.qpos[self._eye_joint_y_mjidx] = 0
            self._mode = self._MODES[0]
            print(f"NOTE, the current mode is: {self._mode}")

        # Sample a target according to the target idx probability distribution
        self._sample_target()

        mujoco.mj_forward(self._model, self._data)

        return self._get_obs()

    def _get_focus(self, site_name):
        site = self._data.site(site_name)
        pnt = site.xpos
        vec = site.xmat.reshape((3, 3))[:, 2]
        # Exclude the body that contains the site, like in the rangefinder sensor
        bodyexclude = self._model.site_bodyid[site.id]
        geomid_out = np.array([-1], np.int32)
        distance = mujoco.mj_ray(
            self._model, self._data, pnt, vec, geomgroup=None, flg_static=1,
            bodyexclude=bodyexclude, geomid=geomid_out)
        return distance, geomid_out[0]

    @staticmethod
    def angle_between(v1, v2):
        # https://stackoverflow.com/questions/2827393/angles-between-two-n-dimensional-vectors-in-python/13849249#13849249
        def unit_vector(vec):
            return vec / np.linalg.norm(vec)

        v1_u = unit_vector(v1)
        v2_u = unit_vector(v2)
        return np.arccos(np.clip(np.dot(v1_u, v2_u), -1.0, 1.0))

    def _angle_from_target(self, site_name, target_idx):
        """
        Return the angle between the vector pointing from the site to the target and the vector pointing from the site to the front
        ranges from 0 to pi.
        """
        # Get vector pointing direction from site
        site = self._data.site(site_name)
        pnt = site.xpos
        vec = pnt + site.xmat.reshape((3, 3))[:, 2]

        # Get vector pointing direction to target
        target_vec = self._data.geom(target_idx).xpos - pnt

        # Estimate distance as angle
        angle = self.angle_between(vec, target_vec)

        return angle

    def _sample_target(self):
        # Sample a target from the cells according to the target idx probability distribution
        new_sampled_target_mjidx = np.random.choice(self._ils100_cells_mjidxs.copy())

        # Make sure the sampled target is different from the previous one
        while True:
            if new_sampled_target_mjidx != self._sampled_target_mjidx:
                break
            else:
                new_sampled_target_mjidx = np.random.choice(self._ils100_cells_mjidxs.copy())
        self._sampled_target_mjidx = new_sampled_target_mjidx

        if self._config["rl"]["mode"] == "test":
            if self._num_trials <= self._max_trials - 1:
                self._sampled_target_mjidx = self._ils100_cells_mjidxs[self._num_trials]

        # Reset the counter
        self._on_target_steps = 0

        # Update the number of remaining unread cells
        self._num_trials += 1

    def step(self, action):
        # Action at t
        # Normalise action from [-1, 1] to actuator control range
        # The control range was set to [-0.7854, 0.7854] as corresponding to [-45, 45] degrees
        # Ref: "Head-fixed saccades can have amplitudes of up to 90°", https://en.wikipedia.org/wiki/Saccade
        action[0] = self.normalise(action[0], -1, 1, *self._model.actuator_ctrlrange[0, :])
        action[1] = self.normalise(action[1], -1, 1, *self._model.actuator_ctrlrange[1, :])

        dist, geomid = self._get_focus(site_name="rangefinder-site")
        if geomid == self._sampled_target_mjidx:
            self._fixate_on_target = True
        else:
            self._fixate_on_target = False
            # Get the ocular motor noise
            amplitude = np.abs(action[0:2].copy() - self._last_step_saccade_qpos[0:2].copy())
            ocular_motor_noise = np.random.normal(0, np.abs(self._rho_ocular_motor * amplitude))

            # print(f"Action before the ocular noise: {action}, "
            #       f"the last step qpos is: {self._last_step_saccade_qpos},"
            #       f"the amplitude is: {amplitude}")

            action[0:2] += ocular_motor_noise

            # print(f"The ocular_motor_noise: {ocular_motor_noise}, corrupted action: {action}")

        self._data.ctrl[0:2] = action[0:2]

        # Save the control for fatigue calculation
        self._ctrl_list.append(self._data.ctrl.copy())

        # Advance the simulation
        mujoco.mj_step(self._model, self._data, self._frame_skip)
        self._steps += 1

        self._last_step_saccade_qpos = self._data.qpos[0:2].copy()

        # State at t+1 - transition function?
        # Eye-sight detection
        dist, geomid = self._get_focus(site_name="rangefinder-site")

        # Reset the scene first
        for mj_idx in self._ils100_cells_mjidxs:
            self._model.geom(mj_idx).rgba = self._DFLT_RGBA
        # Apply the transition function - update the scene regarding the actions
        if geomid == self._sampled_target_mjidx:
            self._on_target_steps += 1
            self._model.geom(self._sampled_target_mjidx).rgba = self._VISUALIZE_RGBA

        # Update the perturbation - firstly try the sinusoidal perturbation
        # With the given perturbation period, the perturbation peak, apply a sinusoidal perturbation
        if self._mode == self._MODES[0]:
            amplitude_z = 0
            amplitude_x = 0
            amplitude_y = 0
        else:
            amplitude_z = self._perturbation_peak_z * np.sin(2 * self._steps / self._perturbation_period_z)
            amplitude_x = self._perturbation_peak_x * np.cos(2 * self._steps / self._perturbation_period_x)
            amplitude_y = - self._perturbation_peak_y * np.cos(2 * self._steps / self._perturbation_period_y)
        self._data.qpos[self._perturbation_joint_z_mjidx] = amplitude_z
        self._data.qpos[self._perturbation_joint_x_mjidx] = amplitude_x
        self._data.qpos[self._perturbation_joint_y_mjidx] = amplitude_y
        mujoco.mj_forward(self._model, self._data)

        # Update the transitions - get rewards and next state
        if self._on_target_steps >= self._dwell_steps:
            # Update the milestone bonus reward for finish reading a cell
            reward = 10
            # Get the next target
            self._sample_target()
        else:
            # TODO - POMDP with a shaking target (should not apply the true)
            reward = 0.1 * (np.exp(
                -10 * self._angle_from_target(site_name="rangefinder-site", target_idx=self._sampled_target_mjidx)) - 1)

        # Get termination condition
        terminate = False
        if self._steps >= self.ep_len or self._num_trials > self._max_trials:
            terminate = True

            if self._config["rl"]["mode"] == "debug" or self._config["rl"]["mode"] == "test":
                print(f"The total time steps is: {self._steps}")
                weight = 1
                print(f"The fatigue cost is: {weight * sum(np.array(self._ctrl_list) ** 2)}, "
                      f"the total value is: {sum(weight * sum(np.array(self._ctrl_list) ** 2))}")

        return self._get_obs(), reward, terminate, {}