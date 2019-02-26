'''
    Play N episodes and get run stats or render one episode for visualization
    using RLLab standards.
'''
# Common imports
import sys, re, os, time, logging
from collections import defaultdict
import pickle as pkl
import argparse

# Framework imports
import gym
import tensorflow as tf
from time import sleep
import numpy as np
from tqdm import trange

# Self imports: utils
from baselines.common import set_global_seeds
import baselines.common.tf_util as U
from rllab.envs.normalized_env import normalize
from baselines.common.rllab_utils import Rllab2GymWrapper, rllab_env_from_name
from baselines.common.atari_wrappers import make_atari, wrap_deepmind
from baselines.common.cmd_util import get_env_type
from rllab.core.network import MLP
# Lasagne
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy
import lasagne.nonlinearities as NL
import lasagne.init as LI
# Self imports: algorithm
from baselines.policy.mlp_policy import MlpPolicy
from baselines.policy.cnn_policy import CnnPolicy
from baselines.pois import pois
from baselines.common.parallel_sampler import ParallelSampler

# ============================
# HARD CODED WEIGHTS
# ============================
WEIGHTS = list(map(lambda x: x.rstrip(' \r\n') if len(x.rstrip(' \r\n')) > 0 else None, """0.05305602 -0.18459691  0.70950599  0.53141615  0.31627853 -0.29742844
 -0.61745293  0.51616656 -0.31569655 -0.33130817  0.40292603 -0.29140672
 -0.57827868 -0.13929554  0.21483592 -0.15595037  0.17454522 -0.19326297
  0.25094717  0.83368002  0.22152866 -0.18484083  0.03383261  0.45452386
 -0.38192054 -0.33433183 -0.03796898 -0.49614562  0.26963035  0.63315435
  0.30569661  0.54124749  0.49595556 -0.22250939 -1.10102485 -0.49956239
 -0.46548248  0.20697958 -1.34049531 -0.30234341  0.26796947  0.23690754
 -0.69478062  1.7125646  -0.67662763  1.93259401  0.21739732  1.49646857
 -0.20361122  0.04088948 -0.21100797 -0.47424338  0.40109244  0.10899712
 -0.35579484  0.13663568  0.08160883  0.29034328  0.6590512  -0.54917494
  0.31953252  1.38984592 -0.09815401  0.05032331 -0.00637668  0.50819017
  0.07671488  0.52851077 -0.17194649 -0.15104466  0.08020897  0.33621285
  0.18527502 -0.19619583 -0.02143036  0.06078397 -0.21261653 -0.01708933
  0.56292861 -0.15359024 -0.43026468  1.67854235 -1.50268328  0.75034172
 -0.5788268   0.26711257 -1.70169838  0.36958352  0.46216935  0.01091633
 -0.83637988 -1.50260334 -0.62852089 -1.48288064 -1.77355002  1.12855729
 -2.63364337""".replace('\n',' ').rstrip(' \r\n').split(' ')))
WEIGHTS = [w for w in WEIGHTS if w is not None]
WEIGHTS = list(map(float, WEIGHTS))
# ============================


def play_episode(env, pi, gamma, horizon=500):
    ob = env.reset()
    done = False
    reward = disc_reward = 0.0
    disc = 1.0
    timesteps = 0
    frames = []
    while not done and timesteps < horizon:
        a, _ = pi.get_action(ob)
        ob, r, done, _ = env.step(0)
        reward += r
        disc_reward += r * disc
        disc *= gamma
        timesteps += 1
        # TODO: add renders
    return {
        'reward': reward,
        'frames': frames
    }

def create_policy_and_env(env, seed, policy, weights):
    # Session
    sess = U.single_threaded_session()
    sess.__enter__()

    # Create env
    env_class = rllab_env_from_name(env)
    env = normalize(env_class())

    # Create policy
    obs_dim = env.observation_space.flat_dim
    action_dim = env.action_space.flat_dim
    if policy == 'linear':
        hidden_sizes = tuple()
    elif policy == 'simple-nn':
        hidden_sizes = [16]
    else:
        raise Exception('NOT IMPLEMENTED.')
    # Creating the policy
    mean_network = MLP(
                input_shape=(obs_dim,),
                output_dim=action_dim,
                hidden_sizes=hidden_sizes,
                hidden_nonlinearity=NL.tanh,
                output_nonlinearity=None,
                output_b_init=None,
                output_W_init=LI.Normal(),
            )
    policy = GaussianMLPPolicy(
        env_spec=env.spec,
        # The neural network policy should have two hidden layers, each with 32 hidden units.
        hidden_sizes=hidden_sizes,
        mean_network=mean_network
    )
    # Set the weights
    if weights is not None:
        raise Exception('TODO load pickle file.')
    else:
        weights = WEIGHTS
    policy.set_param_values(weights)

    print(policy.get_param_values())

    return env, policy

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--seed', help='RNG seed', type=int, default=0)
    parser.add_argument('--env', type=str, default='cartpole')
    parser.add_argument('--policy', type=str, default='nn')
    parser.add_argument('--weights', type=str, default=None, help='Pickle weights file. If None, use hardcoded weights.')
    parser.add_argument('--gamma', type=float, default=1.0)
    parser.add_argument('--output', type=str, default=None)
    parser.add_argument('--episodes', type=int, default=100)
    parser.add_argument('--command', type=str, choices=['stats', 'render'])
    args = parser.parse_args()

    env, pi = create_policy_and_env(args.env, args.seed, args.policy, args.weights)

    N = 1 if args.command == 'render' else args.episodes
    rewards = []
    for i in trange(N):
        stats = play_episode(env, pi, args.gamma)
        rewards.append(stats['reward'])

    if args.command == 'render':
        pass # TODO save frames
    elif args.command == 'stats':
        print("Mean reward:", np.mean(rewards))
    else:
        raise Exception('Unrecognized command.')

if __name__ == '__main__':
    main()
