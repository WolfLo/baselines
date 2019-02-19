#!/usr/bin/env python3
# noinspection PyUnresolvedReferences
'''
    This script runs the RLLab implementation of TRPO on various environments.
    The environments, in this case, are not wrapped for gym. This script
    uses sacred experiment manager.

    export SACRED_RUNS_DIRECTORY to log sacred to a directory
    export SACRED_SLACK_CONFIG to use a slack plugin
'''
# Common imports
import sys, re, os, time, logging
from collections import defaultdict
# RLLab
import rllab
from rllab.algos.trpo import TRPO
from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy
from rllab.envs.normalized_env import normalize
# Baselines
from baselines import logger
from baselines.common.rllab_utils import rllab_env_from_name
from baselines.common.cmd_util import get_env_type

# Sacred
from sacred import Experiment
from sacred.observers import FileStorageObserver, SlackObserver

# Create experiment, assign the name if provided in env variables
if os.environ.get('EXPERIMENT_NAME') is not None:
    ex = Experiment(os.environ.get('EXPERIMENT_NAME'))
else:
    ex = Experiment('POIS')

# Set a File Observer
if os.environ.get('SACRED_RUNS_DIRECTORY') is not None:
    print("Sacred logging at:", os.environ.get('SACRED_RUNS_DIRECTORY'))
    ex.observers.append(FileStorageObserver.create(os.environ.get('SACRED_RUNS_DIRECTORY')))
if os.environ.get('SACRED_SLACK_CONFIG') is not None:
    print("Sacred is using slack.")
    ex.observers.append(SlackObserver.from_config(os.environ.get('SACRED_SLACK_CONFIG')))

@ex.config
def custom_config():
    seed = 0
    env = 'rllab.cartpole'
    num_episodes = 100
    max_iters = 500
    horizon = 500
    file_name = 'progress'
    logdir = 'logs'
    step_size = 0.1
    njobs = -1
    policy = 'nn'
    policy_init = 'xavier'
    gamma = 1.0
    experiment_name = None
    # Create the filename
    if file_name == 'progress':
        file_name = '%s_TRPO_step_size=%s_seed=%s_%s' % (env.upper(), step_size, seed, time.time())
    else:
        file_name = file_name

def train(env, policy, policy_init, num_episodes, horizon, **alg_args):

    # Getting the environment
    env_class = rllab_env_from_name(env)
    env = normalize(env_class())

    # Creating the policy
    if policy == 'linear':
        hidden_sizes = []
    else:
        raise Exception('NOT IMPLEMENTED.')
    policy = GaussianMLPPolicy(env_spec=env.spec, hidden_sizes=hidden_sizes)

    # Creating baseline
    baseline = LinearFeatureBaseline(env_spec=env.spec)

    # Run algorithm
    algo = TRPO(
        env=env,
        policy=policy,
        baseline=baseline,
        batch_size=horizon * num_episodes,
        max_episodes=num_episodes,
        whole_paths=True,
        max_path_length=horizon,
        **alg_args
    )
    algo.train()

@ex.automain
def main(seed, env, num_episodes, horizon, file_name, logdir, step_size, njobs,
            policy, policy_init, gamma, max_iters, _run):

    logger.configure(dir=logdir, format_strs=['stdout', 'csv', 'tensorboard', 'sacred'], file_name=file_name, run=_run)
    train(env=env,
          policy=policy,
          policy_init=policy_init,
          n_episodes=num_episodes,
          horizon=horizon,
          seed=seed,
          njobs=njobs,
          max_iters=max_iters,
          step_size=step_size,
          gamma=gamma)
