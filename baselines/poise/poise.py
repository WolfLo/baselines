import numpy as np
import warnings
import baselines.common.tf_util as U
import tensorflow as tf
import time
from baselines.common import colorize
from baselines.common import zipsame
from contextlib import contextmanager
from collections import deque
from baselines import logger
from baselines.common.cg import cg

@contextmanager
def timed(msg):
    print(colorize(msg, color='magenta'))
    tstart = time.time()
    yield
    print(colorize(
        'done in %.3f seconds' % (time.time() - tstart), color='magenta'))


def traj_segment_generator(pi, env, n_episodes, horizon, stochastic=True):
    """
    Generates a dataset of trajectories
        pi: policy
        env: environment
        n_episodes: batch size
        horizon: max episode length
        stochastic: activates policy stochasticity
    """
    # Initialize state variables
    t = 0
    ac = env.action_space.sample()
    new = True
    ob = env.reset()
    cur_ep_ret = 0
    cur_ep_len = 0

    # Initialize history arrays
    ep_rets = []
    ep_lens = []
    obs = np.array([ob for _ in range(horizon * n_episodes)])
    rews = np.zeros(horizon * n_episodes, 'float32')
    vpreds = np.zeros(horizon * n_episodes, 'float32')
    news = np.zeros(horizon * n_episodes, 'int32')
    acs = np.array([ac for _ in range(horizon * n_episodes)])
    prevacs = acs.copy()
    mask = np.ones(horizon * n_episodes, 'float32')

    # Collect trajectories
    i = 0
    j = 0
    while True:
        prevac = ac
        ac, vpred = pi.act(stochastic, ob)
        if i == n_episodes:
            yield {"ob": obs, "rew": rews, "vpred": vpreds, "new": news,
                   "ac": acs, "prevac": prevacs, "nextvpred": vpred*(1 - new),
                   "ep_rets": ep_rets, "ep_lens": ep_lens, "mask": mask}
            _, vpred = pi.act(stochastic, ob)

            # Reset episode
            ep_rets = []
            ep_lens = []
            mask = np.ones(horizon * n_episodes, 'float32')
            i = 0
            t = 0

        # Update history arrays
        obs[t] = ob
        vpreds[t] = vpred
        news[t] = new
        acs[t] = ac
        prevacs[t] = prevac
        # Transition
        ob, rew, new, _ = env.step(ac)
        rews[t] = rew
        cur_ep_ret += rew
        cur_ep_len += 1
        j += 1

        # Next episode
        if new or j == horizon:
            new = True
            env.done = True
            ep_rets.append(cur_ep_ret)
            ep_lens.append(cur_ep_len)
            cur_ep_ret = 0
            cur_ep_len = 0
            ob = env.reset()
            next_t = (i+1) * horizon
            mask[t+1:next_t] = 0.
            acs[t+1:next_t] = acs[t]
            obs[t+1:next_t] = obs[t]
            t = next_t - 1
            i += 1
            j = 0

        # Next step
        t += 1


def add_disc_rew(seg, gamma):
    """
    Adds discounted rewards and returns to the dataset
        seg: the dataset
        gamma: discount factor
    """
    new = np.append(seg['new'], 1)
    rew = seg['rew']
    n_ep = len(seg['ep_rets'])
    n_samp = len(rew)
    seg['ep_disc_rets'] = ep_disc_ret = np.empty(n_ep, 'float32')
    seg['disc_rew'] = disc_rew = np.empty(n_samp, 'float32')
    discounter = 0
    ret = 0.
    i = 0
    for t in range(n_samp):
        disc_rew[t] = rew[t] * gamma ** discounter
        ret += disc_rew[t]

        if new[t + 1]:
            discounter = 0
            ep_disc_ret[i] = ret
            i += 1
            ret = 0.
        else:
            discounter += 1


def update_epsilon(delta_bound, epsilon_old, max_increase=2.):
    if delta_bound > (1. - 1. / (2 * max_increase)) * epsilon_old:
        return epsilon_old * max_increase
    else:
        return epsilon_old ** 2 / (2 * (epsilon_old - delta_bound))


def line_search_parabola(den_mise, theta_init, alpha, natural_gradient,
                         set_parameter, evaluate_bound, delta_bound_tol=1e-4,
                         max_line_search_ite=30):
    epsilon = 1.
    epsilon_old = 0.
    delta_bound_old = -np.inf
    bound_init = evaluate_bound(den_mise)
    theta_old = theta_init

    for i in range(max_line_search_ite):

        theta = theta_init + epsilon * alpha * natural_gradient
        set_parameter(theta)

        bound = evaluate_bound(den_mise)

        if np.isnan(bound):
            warnings.warn('Got NaN bound value: rolling back!')
            return theta_old, epsilon_old, delta_bound_old, i + 1

        delta_bound = bound - bound_init

        epsilon_old = epsilon
        epsilon = update_epsilon(delta_bound, epsilon_old)
        if delta_bound <= delta_bound_old + delta_bound_tol:
            if delta_bound_old < 0.:
                return theta_init, 0., 0., i+1
            else:
                return theta_old, epsilon_old, delta_bound_old, i+1

        delta_bound_old = delta_bound
        theta_old = theta

    return theta_old, epsilon_old, delta_bound_old, i+1


def optimize_offline(theta, old_thetas_list, set_parameter, set_parameter_old,
                     line_search, evaluate_behav, evaluate_bound,
                     evaluate_gradient, evaluate_natural_gradient=None,
                     gradient_tol=1e-4, bound_tol=1e-4, max_offline_ite=10):

    # Compute MISE's denominator
    den_mise = 0
    for i in range(len(old_thetas_list)):
        set_parameter_old(old_thetas_list[i])
        den_mise += np.exp(evaluate_behav()).astype(np.float32)
    den_mise_log = np.log(den_mise)

    # Print infos about optimization loop
    fmtstr = '%6i %10.3g %10.3g %18i %18.3g %18.3g %18.3g'
    titlestr = '%6s %10s %10s %18s %18s %18s %18s'
    print(titlestr % ('iter', 'epsilon', 'step size', 'num line search',
                      'gradient norm', 'delta bound ite', 'delta bound tot'))

    # Optimization loop
    theta_old = theta
    improvement = improvement_old = 0.
    set_parameter(theta)

    for i in range(max_offline_ite):
        bound = evaluate_bound(den_mise_log)
        gradient = evaluate_gradient(den_mise_log)

        if np.any(np.isnan(gradient)):
            warnings.warn('Got NaN gradient! Stopping!')
            set_parameter(theta_old)
            return theta_old, improvement, den_mise_log

        if np.isnan(bound):
            warnings.warn('Got NaN bound! Stopping!')
            set_parameter(theta_old)
            return theta_old, improvement_old, den_mise_log

        gradient_norm = np.sqrt(np.dot(gradient, gradient))

        if gradient_norm < gradient_tol:
            print('stopping - gradient norm < gradient_tol')
            return theta, improvement, den_mise_log

        alpha = 1. / gradient_norm ** 2

        theta_old = theta
        improvement_old = improvement
        theta, epsilon, delta_bound, num_line_search = \
            line_search(den_mise_log, theta, alpha, gradient,
                        set_parameter, evaluate_bound)
        set_parameter(theta)

        improvement += delta_bound
        print(fmtstr % (i+1, epsilon, alpha*epsilon, num_line_search,
                        gradient_norm, delta_bound, improvement))

    return theta, improvement, den_mise_log

def render(env, pi, horizon):
    """
    Shows a test episode on the screen
        env: environment
        pi: policy
        horizon: episode length
    """
    t = 0
    ob = env.reset()
    env.render()

    done = False
    while not done and t < horizon:
        ac, _ = pi.act(True, ob)
        ob, _, done, _ = env.step(ac)
        time.sleep(0.1)
        env.render()
        t += 1


def learn(make_env, make_policy, *,
          max_iters,
          horizon,
          delta,
          gamma,
          sampler=None,
          iw_norm='none',
          bound='J',
          save_weights=False,
          render_after=None,
          callback=None):
    """
    Learns a policy from scratch
        make_env: environment maker
        make_policy: policy maker
        horizon: max episode length
        delta: probability of failure
        gamma: discount factor
        max_iters: total number of learning iteration
    """

    # Print options
    np.set_printoptions(precision=3)

    # Build the environment
    env = make_env()
    ob_space = env.observation_space
    ac_space = env.action_space
    print('ob_space.shape:', list(ob_space.shape))
    print('ac_space.shape:', list(ac_space.shape))
    max_samples = horizon * max_iters

    # Build the policy
    pi = make_policy('pi', ob_space, ac_space)
    oldpi = make_policy('oldpi', ob_space, ac_space)

    # Get all pi's learnable parameters
    all_var_list = pi.get_trainable_variables()
    var_list = \
        [v for v in all_var_list if v.name.split('/')[1].startswith('pol')]
    shapes = [U.intprod(var.get_shape().as_list()) for var in var_list]
    n_params = sum(shapes)

    # Get all oldpi's learnable parameters
    all_var_list_old = oldpi.get_trainable_variables()
    var_list_old = \
        [v for v in all_var_list_old if v.name.split('/')[1].startswith('pol')]

    # My Placeholders
    old_thetas_ = tf.placeholder(shape=[None, n_params],
                                 dtype=tf.float32, name='old_thetas')
    den_mise_log_ = tf.placeholder(dtype=tf.float32, name='den_mise')

    ob_ = ob = U.get_placeholder_cached(name='ob')  # shape=[None, ac_shape]
    ac_ = pi.pdtype.sample_placeholder([max_samples], name='ac')
    mask_ = tf.placeholder(dtype=tf.float32, shape=(max_samples), name='mask')
    rew_ = tf.placeholder(dtype=tf.float32, shape=(max_samples), name='rew')
    disc_rew_ = tf.placeholder(dtype=tf.float32, shape=(max_samples),
                               name='disc_rew')
    gradient_ = tf.placeholder(dtype=tf.float32,
                               shape=(n_params, 1), name='gradient')
    iter_number_ = tf.placeholder(dtype=tf.float32, name='iter_number')
    losses_with_name = []

    # Policy densities
    target_log_pdf = pi.pd.logp(ac_)
    behavioral_log_pdf = oldpi.pd.logp(ac_)

    # Split operations
    disc_rew_split = tf.stack(tf.split(disc_rew_ * mask_, max_iters))
    rew_split = tf.stack(tf.split(rew_ * mask_, max_iters))
    target_log_pdf_split = tf.stack(
        tf.split(target_log_pdf * mask_, max_iters))
    behavioral_log_pdf_split = tf.stack(
        tf.split(behavioral_log_pdf * mask_, max_iters))
    mask_split = tf.stack(tf.split(mask_, max_iters))

    # Multiple importance weights computation
    print('target_log_pdf_split', target_log_pdf_split.get_shape().as_list())
    target_sum_log = tf.reduce_sum(target_log_pdf_split, axis=1)
    behavioral_sum_log = tf.reduce_sum(behavioral_log_pdf_split, axis=1)
    behavioral_sum_log_sum = tf.reduce_sum(behavioral_sum_log, axis=0)
    behavioral_sum_log_mean = tf.reduce_sum(behavioral_sum_log)/iter_number_
    behavioral_sum_log_centered = behavioral_sum_log - behavioral_sum_log_mean
    print('target_sum_log', target_sum_log.get_shape().as_list())
    print('behavioral_sum_log', behavioral_sum_log.get_shape().as_list())
    # log_ratio_split = target_sum_log - (behavioral_sum_log_mean + den_mise_log_)
    log_ratio_split = target_sum_log - den_mise_log_
    miw = tf.exp(log_ratio_split)

    losses_with_name.extend([(behavioral_sum_log_sum, 'behavioral_sum_log_sum'),
                             (behavioral_sum_log_mean, 'behavioral_sum_log_mean'),
                             (tf.reduce_max(miw), 'MaxIWNorm'),
                             (tf.reduce_min(miw), 'MinIWNorm'),
                             (tf.reduce_mean(miw), 'MeanIWNorm'),
                             (U.reduce_std(miw), 'StdIWNorm'),
                             (tf.reduce_max(miw), 'MaxIW'),
                             (tf.reduce_min(miw), 'MinIW'),
                             (tf.reduce_mean(miw), 'MeanIW'),
                             (U.reduce_std(miw), 'StdIW')])

    # Return
    ep_return = tf.reduce_sum(mask_split * disc_rew_split, axis=1)
    return_mean = tf.reduce_mean(ep_return)
    return_std = U.reduce_std(ep_return)
    return_max = tf.reduce_max(ep_return)
    return_min = tf.reduce_min(ep_return)
    return_abs_max = tf.reduce_max(tf.abs(ep_return))
    return_step_max = tf.reduce_max(tf.abs(rew_split))  # Max step reward
    return_step_mean = tf.abs(tf.reduce_mean(rew_split))
    positive_step_return_max = tf.maximum(0.0, tf.reduce_max(rew_split))
    negative_step_return_max = tf.maximum(0.0, tf.reduce_max(-rew_split))
    return_step_maxmin = tf.abs(
        positive_step_return_max - negative_step_return_max)

    losses_with_name.extend([(return_mean, 'InitialReturnMean'),
                             (return_max, 'InitialReturnMax'),
                             (return_min, 'InitialReturnMin'),
                             (return_std, 'InitialReturnStd'),
                             (return_step_max, 'ReturnStepMax'),
                             (return_step_maxmin, 'ReturnStepMaxmin')])

    # MISE
    mise = tf.reduce_sum(miw * ep_return)
    losses_with_name.append((mise, 'MISE'))

    # Renyi divergence
    if bound == 'J':
        bound_ = mise
    elif bound == 'max-ess':
        sqrt_ess_classic = tf.linalg.norm(miw, 1) / tf.linalg.norm(miw, 2)
        mise_variance = \
            tf.sqrt((1 - delta) / delta) / sqrt_ess_classic * return_abs_max
        bound_ = mise + mise_variance
        losses_with_name.append((sqrt_ess_classic, 'SqrtESSClassic'))
    losses_with_name.append((bound_, 'Bound'))

    # Infos
    assert_ops = tf.group(*tf.get_collection('asserts'))
    print_ops = tf.group(*tf.get_collection('prints'))
    losses, loss_names = map(list, zip(*losses_with_name))

    # TF functions
    set_parameter = U.SetFromFlat(var_list)
    get_parameter = U.GetFlat(var_list)
    set_parameter_old = U.SetFromFlat(var_list_old)
    get_parameter_old = U.GetFlat(var_list_old)

    compute_behav = U.function(
        [ob_, ac_, mask_, iter_number_],
        behavioral_sum_log,
        updates=None, givens=None)
    compute_miw = U.function(
        [ob_, ac_, mask_, den_mise_log_],
        miw, updates=None, givens=None)
    compute_bound = U.function(
        [ob_, ac_, rew_, disc_rew_, mask_, iter_number_, den_mise_log_],
        [bound_, assert_ops, print_ops])
    compute_grad = U.function(
        [ob_, ac_, rew_, disc_rew_, mask_, iter_number_, den_mise_log_],
        [U.flatgrad(bound_, var_list), assert_ops, print_ops])
    compute_losses = U.function(
        [ob_, ac_, rew_, disc_rew_, mask_, iter_number_, den_mise_log_],
        losses)

    # Set sampler (default: sequential)
    if sampler is None:
        # Rb:collect only ONE trajectory
        seg_gen = traj_segment_generator(pi, env, 1,
                                         horizon, stochastic=True)
        sampler = type("SequentialSampler", (object,),
                       {"collect": lambda self, _: seg_gen.__next__()})()

    # Tf initialization
    U.initialize()

    # Learning loop
    episodes_so_far = 0
    timesteps_so_far = 0
    iters_so_far = 0
    tstart = time.time()
    # Store behaviorals' params and their trajectories
    old_thetas_list = []
    all_seg = {}
    all_seg['ob'] = np.zeros((max_samples, ob_space.shape[0]))
    all_seg['ac'] = np.zeros(shape=ac_.get_shape().as_list())

    for i in ["rew", "disc_rew", "mask"]:
        all_seg[i] = np.zeros(max_samples)

    while True:
        iters_so_far += 1

        # Render one episode
        if render_after is not None and iters_so_far % render_after == 0:
            if hasattr(env, 'render'):
                render(env, pi, horizon)

        # Custom callback
        if callback:
            callback(locals(), globals())

        # Exit loop in the end
        if iters_so_far >= max_iters:
            print('Finished...')
            break

        # Learning iteration
        logger.log('********** Iteration %i ************' % iters_so_far)
        # Store the list of arrays representing pi's parameters

        # Generate trajectories
        theta = get_parameter()
        with timed('sampling'):
            seg = sampler.collect(theta)

        # Store the list of arrays representing behaviorals' parameters
        old_thetas_list.append(theta)

        # Retrieve data
        add_disc_rew(seg, gamma)
        lens, u_rets = seg['ep_lens'], seg['ep_rets']
        assert len(lens) == 1
        episodes_so_far += 1
        timesteps_so_far += lens[0]

        args = ()
        for key in all_seg.keys():
            start = (iters_so_far-1)*horizon
            all_seg[key][start:start + horizon] = seg[key]
        print('all_seg[mask] non-zeros', all_seg['mask'])
        args = all_seg['ob'], all_seg['ac'],  all_seg['rew'], \
            all_seg['disc_rew'], all_seg['mask'], iters_so_far
        # Info
        with timed('summaries before'):
            logger.record_tabular("Iteration", iters_so_far)
            logger.record_tabular("URet", u_rets[0])
            logger.record_tabular("TimestepsSoFar", timesteps_so_far)
            logger.record_tabular("TimeElapsed", time.time() - tstart)

        # Save policy parameters to disk
        # if save_weights:
        #     logger.record_tabular('Weights', str(get_parameter()))
        #     import pickle
        #     file = open('checkpoint.pkl', 'wb')
        #     pickle.dump(theta, file)

        def evaluate_behav():
            args_behav = all_seg['ob'], all_seg['ac'], \
                all_seg['mask'], iters_so_far
            return compute_behav(*args_behav)

        def evaluate_miw(den_mise_log):
            args_miw = all_seg['ob'], all_seg['ac'], \
                all_seg['mask'], den_mise_log
            return compute_miw(*args_miw)

        def evaluate_bound(den_mise_log):
            args_bound = args + (den_mise_log,)
            return compute_bound(*args_bound)[0]

        def evaluate_gradient(den_mise_log):
            args_gradient = args + (den_mise_log,)
            return compute_bound(*args_gradient)[0]

        # Perform optimization
        line_search = line_search_parabola
        with timed("Optimization"):
            theta, improvement, den_mise_log = \
                optimize_offline(theta, old_thetas_list,
                                 set_parameter, set_parameter_old,
                                 line_search,
                                 evaluate_behav, evaluate_bound,
                                 evaluate_gradient)
        args += (den_mise_log,)
        set_parameter(theta)

        # Info
        with timed('summaries after'):
            meanlosses = np.array(compute_losses(*args))
            for (lossname, lossval) in zip(loss_names, meanlosses):
                logger.record_tabular(lossname, lossval)

        # Print all info in a table
        logger.dump_tabular()

    # Close environment in the end
    env.close()
