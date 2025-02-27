import os

import yaml
from tqdm import tqdm
import numpy as np

import gym

import torch as th
from torch import nn

from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import SubprocVecEnv, DummyVecEnv
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.vec_env import VecFrameStack
from stable_baselines3.common.callbacks import CheckpointCallback
from stable_baselines3.common.evaluation import evaluate_policy
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.type_aliases import TensorDict
from stable_baselines3.common.preprocessing import get_flattened_obs_dim, is_image_space

from ZigzagReadingEnv import ZigzagReadingEnv
from utils import write_video

_MODES = {
    'train': 'train',
    'continual_train': 'continual_train',
    'test': 'test',
    'debug': 'debug',
    'interact': 'interact'
}


class CustomCNN(BaseFeaturesExtractor):

    def __init__(self, observation_space: gym.spaces.Box, features_dim: int = 128):
        """
        The custom cnn feature extractor.
        Ref: https://stable-baselines3.readthedocs.io/en/master/guide/custom_policy.html#custom-feature-extractor
        :param observation_space: (gym.Space)
        :param features_dim: (int) Number of features extracted.
            This corresponds to the number of unit for the last layer.
        """
        super().__init__(observation_space, features_dim)
        # We assume CxHxW images (channels first)
        # Re-ordering will be done by pre-preprocessing or wrapper
        n_input_channels = observation_space.shape[0]
        self.cnn = nn.Sequential(
            # 3*80*80 -> (80+2*1-4)/2+1=40 8*40*40
            nn.Conv2d(in_channels=n_input_channels, out_channels=8, kernel_size=(4, 4), padding=(1, 1), stride=(2, 2)),
            nn.LeakyReLU(),
            # 8*40*40 -> (40+2*1-4)/2+1=20 16*20*20
            nn.Conv2d(in_channels=8, out_channels=16, kernel_size=(4, 4), padding=(1, 1), stride=(2, 2)),
            nn.LeakyReLU(),
            # 16*20*20 -> (20+2*1-4)/2+1=10 32*10*10
            nn.Conv2d(in_channels=16, out_channels=32, kernel_size=(4, 4), padding=(1, 1), stride=(2, 2)),
            nn.LeakyReLU(),
            # 32*10*10 -> (10+2*1-4)/2+1=5 64*5*5
            nn.Conv2d(in_channels=32, out_channels=64, kernel_size=(4, 4), padding=(1, 1), stride=(2, 2)),
            nn.LeakyReLU(),
            # If the original input was 1*3*80*80; now the output is 1*64*5*5.
            nn.Flatten(),
            # After flatten, the dimension is 1*1600.
        )

        # Compute shape by doing one forward pass。
        with th.no_grad():
            n_flatten = self.cnn(th.as_tensor(observation_space.sample()[None]).float()).shape[1]

        self.linear = nn.Sequential(nn.Linear(n_flatten, features_dim), nn.LeakyReLU())

    def forward(self, observations: th.Tensor) -> th.Tensor:
        return self.linear(self.cnn(observations))


class CustomCombinedExtractor(BaseFeaturesExtractor):
    def __init__(self, observation_space: gym.spaces.Dict, cnn_output_dim: int = 128):
        """
        Custom multi-input features extractor.
        Ref: https://stable-baselines3.readthedocs.io/en/master/guide/custom_policy.html#multiple-inputs-and-dictionary-observations
        Ref: CombinedExtractor
        """
        super().__init__(observation_space, features_dim=1)

        extractors = {}

        total_concat_size = 0

        for key, subspace in observation_space.spaces.items():
            if key == 'rgb':
                # Assume that the input observation rgb pixels' shape is 3, 80, 80
                extractors[key] = CustomCNN(subspace, features_dim=cnn_output_dim)
                total_concat_size += cnn_output_dim
            elif key == 'fix_waste_steps' or key == 'pre_now_focus':
                # Run through a simple MLP.
                out_features = 16
                if isinstance(subspace, gym.spaces.box.Box):
                    in_features = subspace.shape[0]
                elif isinstance(subspace, gym.spaces.multi_discrete.MultiDiscrete):
                    in_features = np.sum(subspace.nvec)
                else:
                    in_features = 0
                    raise ValueError('The in_features needs to be specified - noted by BYP')
                extractors[key] = nn.Linear(in_features=in_features, out_features=out_features)
                total_concat_size += out_features

                # The observation key is a vector, flatten it if needed.
                # extractors[key] = nn.Flatten()
                # total_concat_size += get_flattened_obs_dim(subspace)

        self.extractors = nn.ModuleDict(extractors)

        # Update the features dim manually. self._features_dim is used to initialize the features dimension of the base class BaseFeaturesExtractor.
        self._features_dim = total_concat_size

    def forward(self, observations: TensorDict) -> th.Tensor:
        encoded_tensor_list = []

        for key, extractor in self.extractors.items():
            encoded_tensor_list.append(extractor(observations[key]))
        return th.cat(encoded_tensor_list, dim=1)


class RL:
    def __init__(self, config_file):
        """
        This is the reinforcement learning pipeline where MuJoCo environments are created, and models are trained and tested.
        This pipeline is derived from my trials: context_switch.

        Args:
            config_file: the YAML configuration file that records the configurations.
        """
        # Read the configurations from the YAML file.
        with open(config_file) as f:
            config = yaml.load(f, Loader=yaml.FullLoader)

        try:
            self._config_rl = config['rl']
            self._config_mj_env = config['mj_env']
            self._config_utils = config['utils']
        except ValueError:
            print('Invalid configurations. Check your config.yaml file.')

        # Specify the pipeline mode.
        self._mode = self._config_rl['mode']

        # Get an env instance for further constructing parallel environments.
        self._env = ZigzagReadingEnv(config=config)

        # Initialise parallel environments.
        self._parallel_envs = make_vec_env(
            env_id=self._env.__class__,
            env_kwargs={'config': config},
            n_envs=self._config_rl['train']["num_workers"],
            seed=None,
            vec_env_cls=SubprocVecEnv,
        )

        # Identify the modes and specify corresponding initiates. TODO add valueError raisers as insurance + refine this part later
        # Train the model, and save the logs and modes at each checkpoints.
        if self._mode == _MODES['train']:
            # Pipeline related variables.
            self._training_logs_path = os.path.join('training', 'logs')
            self._checkpoints_folder_name = self._config_rl['train']['checkpoints_folder_name']
            self._models_save_path = os.path.join('training', 'saved_models', self._checkpoints_folder_name)
            self._models_save_file_final = os.path.join(self._models_save_path, self._config_rl['train']['checkpoints_folder_name'])
            # RL training related variable: total time-steps.
            self._total_timesteps = self._config_rl['train']['total_timesteps']
            # Configure the model.
            # Initialise model that is run with multiple threads.
            policy_kwargs = dict(
                features_extractor_class=CustomCombinedExtractor,
                # features_extractor_kwargs=dict(features_dim=128),
                activation_fn=th.nn.LeakyReLU,
                net_arch=[128, 128],
            )
            self._model = PPO(
                policy=self._config_rl['train']["policy_type"],
                env=self._parallel_envs,
                verbose=1,
                policy_kwargs=policy_kwargs,
                # self._config_rl_pipeline["policy_kwargs"],    # TODO insert into the config file later.
                tensorboard_log=self._training_logs_path,
                n_steps=self._config_rl['train']["num_steps"],
                batch_size=self._config_rl['train']["batch_size"],
                device=self._config_rl['train']["device"]
            )
        # Load the pre-trained models and test. Load the pre-trained models and test.
        elif self._mode == _MODES['test'] or self._mode == _MODES['continual_train']:
            # Pipeline related variables.
            self._loaded_model_name = self._config_rl['test']['loaded_model_name']
            self._checkpoints_folder_name = self._config_rl['train']['checkpoints_folder_name']
            self._models_save_path = os.path.join('training', 'saved_models', self._checkpoints_folder_name)
            self._loaded_model_path = os.path.join(self._models_save_path, self._loaded_model_name)
            # RL testing related variable: number of episodes and number of steps in each episodes.
            self._num_episodes = self._config_rl['test']['num_episodes']
            self._num_steps = self._env.num_steps
            # Load the model
            if self._mode == _MODES['test']:
                self._model = PPO.load(self._loaded_model_path, self._env)
            elif self._mode == _MODES['continual_train']:
                # Logistics.
                # Pipeline related variables.
                self._training_logs_path = os.path.join('training', 'logs')
                self._checkpoints_folder_name = self._config_rl['train']['checkpoints_folder_name']
                self._models_save_path = os.path.join('training', 'saved_models', self._checkpoints_folder_name)
                self._models_save_file_final = os.path.join(self._models_save_path,
                                                            self._config_rl['train']['checkpoints_folder_name'])
                # RL training related variable: total time-steps.
                self._total_timesteps = self._config_rl['train']['total_timesteps']
                # Model loading and register.
                self._model = PPO.load(self._loaded_model_path)
                self._model.set_env(self._parallel_envs)
        # The MuJoCo environment debugs. Check whether the environment and tasks work as designed.
        elif self._mode == _MODES['debug']:
            self._num_episodes = self._config_rl['test']['num_episodes']
            self._num_steps = self._env.num_steps
        # The MuJoCo environment demo display with user interactions, such as mouse interactions.
        elif self._mode == _MODES['interact']:
            pass
        else:
            pass

    def _train(self):
        """Add comments """
        # Save a checkpoint every certain steps, which is specified by the configuration file.
        # Ref: https://stable-baselines3.readthedocs.io/en/master/guide/callbacks.html
        # To account for the multi-envs' steps, save_freq = max(save_freq // n_envs, 1).
        save_freq = self._config_rl['train']['save_freq']
        n_envs = self._config_rl['train']['num_workers']
        save_freq = max(save_freq // n_envs, 1)
        checkpoint_callback = CheckpointCallback(
            save_freq=save_freq,
            save_path=self._models_save_path,
            name_prefix='rl_model',
            save_replay_buffer=True,
            save_vecnormalize=True,
        )

        # Train the RL model and save the logs. The Algorithm and policy were given,
        # but it can always be upgraded to a more flexible pipeline later.
        self._model.learn(
            total_timesteps=self._total_timesteps,
            callback=checkpoint_callback,
            # progress_bar=True
        )

        # Save the model as the rear guard.
        self._model.save(self._models_save_file_final)

    def _continual_train(self):
        """
        This method perform the continual trainings.
        Ref: https://github.com/hill-a/stable-baselines/issues/599#issuecomment-569393193
        """
        save_freq = self._config_rl['train']['save_freq']
        n_envs = self._config_rl['train']['num_workers']
        save_freq = max(save_freq // n_envs, 1)
        checkpoint_callback = CheckpointCallback(
            save_freq=save_freq,
            save_path=self._models_save_path,
            name_prefix='rl_model_continual',
            save_replay_buffer=True,
            save_vecnormalize=True,
        )

        self._model.learn(
            total_timesteps=self._total_timesteps,
            callback=checkpoint_callback,
            log_interval=1,
            tb_log_name=self._config_rl['test']['continual_logs_name'],
            reset_num_timesteps=False,
        )
        print(os.path.join(self._training_logs_path, self._config_rl['test']['continual_logs_name']))   # TODO debug, delete later.

        # Save the model as the rear guard.
        self._model.save(self._models_save_file_final)

    def _test(self):
        """
        This method generates the RL env testing results with or without a pre-trained RL model in a manual way.
        """
        if self._mode == _MODES['debug']:
            print('\nThe MuJoCo env and tasks baseline: ')
        elif self._mode == _MODES['test']:
            print('\nThe pre-trained RL model testing: ')

        for episode in range(1, self._num_episodes + 1):
            obs = self._env.reset()
            done = False
            score = 0
            info = None
            progress_bar = tqdm(total=self._num_steps)
            while not done:
                if self._mode == _MODES['debug']:
                    action = self._env.action_space.sample()
                elif self._mode == _MODES['test']:
                    action, _states = self._model.predict(obs)
                else:
                    action = 0
                obs, reward, done, info = self._env.step(action)
                score += reward
                progress_bar.update(1)
            progress_bar.close()  # Tip: this line's better before any update. Or it would be split.
            print(
                '\nEpisode:{}     Score:{}    '
                '\nLoops Pct: {}%     Optimal Loops: {}     Actual Loops: {}'
                .format(episode, score,
                        np.round(info['achievement']*100, 2), info['optimal_loops'], info['num_loops'])
            )

        if self._mode == _MODES['test']:
            # Use the official evaluation tool. | TODO check if needs to make a registered env instead of using the parallel envs.
            evl = evaluate_policy(self._model, self._parallel_envs, n_eval_episodes=self._num_episodes, render=False)
            print('The evaluation results are: Mean {}; STD {}'.format(evl[0], evl[1]))

    def run(self):
        """
        This method helps run the RL pipeline.
        Call it.
        """
        print(
            '\n\n***************************** RL Pipeline starts with the mode {} *************************************'
                .format(self._mode)
        )
        # Check train or not.
        if self._mode == _MODES['train']:
            self._train()
        elif self._mode == _MODES['continual_train']:
            self._continual_train()
        elif self._mode == _MODES['test']:
            # Generate the results from the pre-trained model.
            self._test()

            # Write a video. First get the rgb images, then identify the path.
            video_folder_path = os.path.join('training', 'videos')  # TODO the resolution needs bigger, the visual actions needs to match. And maybe lower fps.
            if os.path.exists(video_folder_path) is False:
                os.makedirs(video_folder_path)
            video_path = os.path.join(video_folder_path, self._loaded_model_name + '.avi')
            write_video.write_video(
                filepath=video_path,
                fps=self._config_utils['write_video']['fps'],
                rgb_images=self._env.rgb_images,
                width=self._config_mj_env['render']['width'],
                height=self._config_mj_env['render']['height'],
            )
        elif self._mode == _MODES['debug']:
            # Generate the baseline.
            self._test()
        else:
            # TODO specify the demo mode later.
            #  Should be something like: self._env.demo()
            pass

    def __del__(self):
        # Close the environment.
        self._env.close()

        # Visualize the destructor.
        print(
            '\n\n***************************** RL pipeline ends. The MuJoCo environment of the pipeline has been destructed *************************************'
        )
