from baselines.ppo1.mlp_policy import MlpPolicy
from ifqi.envs.lqg1d import LQG1D
import numpy as np
import baselines.common.tf_util as U

sess = U.single_threaded_session()
sess.__enter__()

env = LQG1D()

pi = MlpPolicy("pi",env.observation_space,env.action_space,hid_size=1,
               num_hid_layers=1,use_bias=False)

be = MlpPolicy("be",env.observation_space,env.action_space,hid_size=1,
               num_hid_layers=0,use_bias=False)
