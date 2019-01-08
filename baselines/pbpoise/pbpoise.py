import os
import matplotlib as mpl
if os.environ.get('DISPLAY', '') == '':
    print('no display found. Using non-interactive Agg backend')
    mpl.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits import mplot3d
import numpy as np
import baselines.common.tf_util as U
import tensorflow as tf
import time
from baselines.common import colorize
from contextlib import contextmanager
from baselines import logger
from plotting_tools import plot3D_bound_profile, plot_bound_profile, render


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


def eval_trajectory(env, pol, gamma, horizon, feature_fun):
    ret = disc_ret = 0
    t = 0
    ob = env.reset()
    done = False
    while not done and t < horizon:
        s = feature_fun(ob) if feature_fun else ob
        a = pol.act(s)
        ob, r, done, _ = env.step(a)
        ret += r
        disc_ret += gamma**t * r
        t += 1

    return ret, disc_ret, t

def update_epsilon(delta_bound, epsilon_old, max_increase=2.):
    if delta_bound > (1. - 1. / (2 * max_increase)) * epsilon_old:
        return epsilon_old * max_increase
    else:
        return epsilon_old**2 / (2 * (epsilon_old - delta_bound))


def line_search_parabola(den_mise, rho_init, alpha, natural_grad,
                         set_parameters, evaluate_bound, drho,
                         iters_so_far, delta_bound_tol=1e-4,
                         max_line_search_ite=30):
    epsilon = 1.
    epsilon_old = 0.
    delta_bound_old = -np.inf
    bound_init = evaluate_bound(den_mise)
    rho_old = rho_init

    for i in range(max_line_search_ite):

        rho = rho_init + epsilon * alpha * natural_grad
        set_parameters(rho)

        bound = evaluate_bound(den_mise)

        if np.isnan(bound):
            print('Got NaN bound value: rolling back!')
            return rho_old, epsilon_old, 0, i + 1

        delta_bound = bound - bound_init

        epsilon_old = epsilon
        epsilon = update_epsilon(delta_bound, epsilon_old)
        if delta_bound <= delta_bound_old + delta_bound_tol:
            if delta_bound_old < 0.:
                return rho_init, 0., 0., i+1
            else:
                return rho_old, epsilon_old, delta_bound_old, i+1

        delta_bound_old = delta_bound
        rho_old = rho

    return rho_old, epsilon_old, delta_bound_old, i+1


def best_of_grid(policy, grid_size,
                 rho_init, old_rhos_list,
                 iters_so_far, mask_iters,
                 set_parameters, set_parameters_old,
                 delta_cst,
                 evaluate_behav, evaluate_bound,
                 evaluate_renyi, evaluate_roba,
                 filename):

    threeDplot = False

    # Compute MISE's denominator and Renyi bound
    den_mise = np.zeros(mask_iters.shape).astype(np.float32)
    # renyi_components_sum = 0
    for i in range(len(old_rhos_list)):
        set_parameters_old(old_rhos_list[i])
        behav = evaluate_behav()
        den_mise = den_mise + np.exp(behav)

    # Compute the log of MISE's denominator
    eps = 1e-24  # to avoid inf weights and nan bound
    den_mise = (den_mise + eps) / iters_so_far
    den_mise_log = np.log(den_mise) * mask_iters

    # Calculate the grid of parameters to evaluate
    gain_grid = np.linspace(-1, 1, grid_size)
    logstd_grid = np.linspace(-20, 0, grid_size)
    if len(rho_init) == 2:
        threeDplot = True
        x, y = np.meshgrid(gain_grid, logstd_grid)
        X = x.reshape((np.prod(x.shape),))
        Y = y.reshape((np.prod(y.shape),))
        rho_grid = list(zip(X, Y))
    else:
        rho_grid = [[x] for x in gain_grid]
    # Evaluate the set of parameters and retain the best one
    bound = []
    mise = []
    bonus = []
    bound_best = 0
    renyi_bound_best = 0
    rho_best = rho_init

    for rho in rho_grid:
        # rho = np.array(rho)
        set_parameters(rho)
        renyi_components_sum = 0
        for i in range(len(old_rhos_list)):
            set_parameters_old(old_rhos_list[i])
            renyi_component = evaluate_renyi()
            renyi_components_sum += 1 / renyi_component
        renyi_bound = 1 / renyi_components_sum
        bound_rho = evaluate_bound(den_mise_log, renyi_bound)
        bound.append(bound_rho)
        mise_rho, bonus_rho = \
            evaluate_roba(den_mise_log, renyi_bound)
        mise.append(mise_rho)
        bonus.append(bonus_rho)

        if bound_rho > bound_best:
            bound_best = bound_rho
            rho_best = rho
            renyi_bound_best = renyi_bound

    # Plot the profile of the bound and its components
    if threeDplot:
        bound = np.array(bound).reshape((grid_size, grid_size))
        plot3D_bound_profile(x, y, bound, rho_best, bound_best,
                             iters_so_far, filename)
    else:
        plot_bound_profile(gain_grid, bound, mise, bonus, rho_best[0],
                           bound_best, iters_so_far, filename)

    # Calculate improvement
    set_parameters(rho_init)
    improvement = bound_best - evaluate_bound(den_mise_log, renyi_bound)

    return rho_best, improvement, den_mise_log, renyi_bound


def optimize_offline(evaluate_roba, pi,
                     rho_init, drho, old_rhos_list,
                     iters_so_far, mask_iters,
                     set_parameters, set_parameters_old,
                     evaluate_behav, evaluate_renyi,
                     evaluate_bound, evaluate_grad,
                     line_search, evaluate_natural_grad=None,
                     grad_tol=1e-4, bound_tol=1e-10, max_offline_ite=10):

    # Compute MISE's denominator and Renyi bound
    den_mise = np.zeros(mask_iters.shape).astype(np.float32)
    renyi_components_sum = 0
    for i in range(len(old_rhos_list)):
        set_parameters_old(old_rhos_list[i])
        behav = evaluate_behav()
        den_mise = den_mise + np.exp(behav)
        renyi_component = evaluate_renyi()
        renyi_components_sum += 1 / renyi_component
    renyi_bound = 1 / renyi_components_sum
    renyi_bound_old = renyi_bound

    # Compute the log of MISE's denominator
    eps = 1e-24  # to avoid inf weights and nan bound
    den_mise = (den_mise + eps) / iters_so_far
    den_mise_log = np.log(den_mise) * mask_iters

    # Set optimization variables
    rho = rho_old = rho_init
    improvement = improvement_old = 0.
    num_line_search = 0

    # Calculate initial bound
    bound = evaluate_bound(den_mise_log, renyi_bound)
    if np.isnan(bound):
        print('Got NaN bound! Stopping!')
        set_parameters(rho_old)
        return rho, improvement, den_mise_log, bound
    bound_old = bound
    print('Initial bound after last sampling:', bound)

    # Print infos about optimization loop
    fmtstr = '%6i %10.3g %16i %18.3g %18.3g %18.3g %18.3g'
    titlestr = '%6s %10s  %18s %16s %18s %18s %18s'
    print(titlestr % ('iter', 'step size', 'line searches', 'grad norm',
                      'delta rho', 'delta bound ite', 'delta bound tot'))

    # Optimization loop
    if max_offline_ite > 0:
        for i in range(max_offline_ite):

            # Gradient
            grad = evaluate_grad(den_mise_log, renyi_bound)
            # Sanity check for the grad
            if np.any(np.isnan(grad)):
                print('Got NaN grad! Stopping!')
                set_parameters(rho_old)
                return rho_old, improvement, den_mise_log, bound_old

            # Natural gradient
            if pi.diagonal:
                natgrad = grad / (pi.eval_fisher() + 1e-24)
            else:
                raise NotImplementedError
            assert np.dot(grad, natgrad) >= 0

            grad_norm = np.sqrt(np.dot(grad, natgrad))
            # Check that the grad norm is not too small
            if grad_norm < grad_tol:
                print('stopping - grad norm < grad_tol')
                # print('rho', rho)
                # print('rho_old', rho_old)
                # print('rho_init', rho_init)
                return rho, improvement, den_mise_log, bound, renyi_bound

            # Save old values
            rho_old = rho
            improvement_old = improvement
            # Make an optimization step
            alpha = drho / grad_norm**2
            if line_search is not None:
                rho, epsilon, delta_bound, num_line_search = \
                    line_search(den_mise_log, rho, alpha, natgrad,
                                set_parameters, evaluate_bound,
                                iters_so_far, bound_tol)
                set_parameters(rho)
                delta_rho = np.array(rho) - np.array(rho_old)
            else:
                delta_rho = alpha * natgrad
                rho = rho + delta_rho
                set_parameters(rho)
                # Update bounds
                renyi_components_sum = 0
                for i in range(len(old_rhos_list)):
                    set_parameters_old(old_rhos_list[i])
                    renyi_component = evaluate_renyi()
                    renyi_components_sum += 1 / renyi_component
                renyi_bound = 1 / renyi_components_sum
                # Sanity check for the bound
                bound = evaluate_bound(den_mise_log, renyi_bound)
                if np.isnan(bound):
                    print('Got NaN bound! Stopping!')
                    set_parameters(rho_old)
                    return rho_old, improvement_old, den_mise_log, \
                        renyi_bound_old, bound_old
                delta_bound = bound - bound_old

            improvement = improvement + delta_bound
            bound_old = bound
            renyi_bound_old = renyi_bound

            print(fmtstr % (i+1, alpha, num_line_search, grad_norm,
                            delta_rho[0], delta_bound, improvement))

    print('Max number of offline iterations reached.')
    return rho, improvement, den_mise_log, renyi_bound, bound


def learn(make_env, make_policy, *,
          max_iters,
          horizon,
          drho,
          delta,
          gamma,
          multiple_init=None,
          sampler=None,
          feature_fun=None,
          iw_norm='none',
          bound_type='max-ess',
          max_offline_iters=10,
          save_weights=False,
          render_after=None,
          line_search=None,
          grid_optimization=None,
          truncated_mise=True,
          delta_t=None,
          filename=None):
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

    # Build the higher level target and behavioral policies
    pi = make_policy('pi', ob_space, ac_space)
    oldpi = make_policy('oldpi', ob_space, ac_space)

    # Get all pi's learnable parameters
    all_var_list = pi.get_trainable_variables()
    var_list = \
        [v for v in all_var_list if v.name.split('/')[1].startswith('higher')]
    shapes = [U.intprod(var.get_shape().as_list()) for var in var_list]
    d = sum(shapes)

    # Get all oldpi's learnable parameters
    all_var_list_old = oldpi.get_trainable_variables()
    var_list_old = \
        [v for v in all_var_list_old
         if v.name.split('/')[1].startswith('higher')]

    # My Placeholders
    actor_params_ = tf.placeholder(shape=[max_iters, pi._n_actor_weights],
                                   name='actor_params', dtype=tf.float32)
    den_mise_log_ = tf.placeholder(shape=[max_iters], dtype=tf.float32,
                                   name='den_mise')
    renyi_bound_ = tf.placeholder(dtype=tf.float32, name='renyi_bound')
    ret_ = tf.placeholder(dtype=tf.float32, shape=(max_iters), name='ret')
    disc_ret_ = tf.placeholder(dtype=tf.float32, shape=(max_iters),
                               name='disc_ret')
    n_ = tf.placeholder(dtype=tf.float32, name='iter_number')
    n_int = tf.cast(n_, dtype=tf.int32)
    mask_iters_ = tf.placeholder(dtype=tf.float32, shape=(max_iters),
                                 name='mask_iters')
    losses_with_name = []
    # grad_ = tf.placeholder(dtype=tf,.float32,
    #                            shape=(d, 1), name='grad')

    # Multiple importance weights (with balance heuristic)
    target_log_pdf = tf.reduce_sum(
        pi.pd.independent_logps(actor_params_), axis=1)
    behavioral_log_pdf = tf.reduce_sum(
        oldpi.pd.independent_logps(actor_params_), axis=1)
    log_ratio = target_log_pdf - den_mise_log_
    miw = tf.exp(log_ratio) * mask_iters_

    den_mise_log_mean = tf.reduce_sum(den_mise_log_) / n_
    den_mise_log_last = den_mise_log_[n_int-1]
    losses_with_name.extend([(den_mise_log_mean, 'DenMISEMeanLog'),
                             (den_mise_log_[0], 'DenMISELogFirst'),
                             (den_mise_log_last, 'DenMISELogLast'),
                             (miw[0], 'IWFirstEpisode'),
                             (miw[n_int-1], 'IWLastEpisode'),
                             (tf.reduce_sum(miw)/n_, 'IWMean'),
                             (tf.reduce_max(miw), 'IWMax'),
                             (tf.reduce_min(miw), 'IWMin')])

    # Return
    ep_return = disc_ret_
    return_mean = tf.reduce_sum(ep_return) / n_
    return_last = ep_return[n_int - 1]
    return_max = tf.reduce_max(ep_return)
    return_min = tf.reduce_min(ep_return)
    return_abs_max = tf.reduce_max(tf.abs(ep_return))
    return_step_max = tf.reduce_max(tf.abs(ret_))

    losses_with_name.extend([(return_mean, 'ReturnMean'),
                             (return_max, 'ReturnMax'),
                             (return_min, 'ReturnMin'),
                             (return_last, 'ReturnLastEpisode'),
                             (return_abs_max, 'ReturnAbsMax'),
                             (return_step_max, 'ReturnStepMax')])

    # Exponentiated Renyi divergence between the target and one behavioral
    renyi_component = pi.pd.renyi(oldpi.pd)
    renyi_component = tf.cond(tf.is_nan(renyi_component),
                              lambda: tf.constant(np.inf),
                              lambda: renyi_component)
    renyi_component = tf.cond(renyi_component < 0.,
                              lambda: tf.constant(np.inf),
                              lambda: renyi_component)
    renyi_component = tf.exp(renyi_component)

    if truncated_mise:
        # Bound to d2(target || mixture of behaviorals)/n
        mn = tf.sqrt((n_**2 * renyi_bound_) / tf.log(1 / delta))
        mn_broadcasted = \
            tf.ones(shape=miw.get_shape().as_list(), dtype=np.float32) * mn
        min = tf.where(tf.less(miw, mn_broadcasted), miw, mn_broadcasted)
        mise = tf.reduce_sum(min * ep_return * mask_iters_)/n_
    else:
        # MISE
        mise = tf.reduce_sum(miw * ep_return * mask_iters_)/n_
        losses_with_name.append((mise, 'MISE'))

    # Bounds
    if delta_t == 'continuous':
        delta_cst = delta
        delta = 6 * delta / ((np.pi * n_)**2 * (1 + (d * n_**2)**d))
    elif delta_t == 'discrete':
        delta_cst = delta
        delta = 3 * delta / ((np.pi * n_)**2 * grid_optimization)
    elif delta_t is None:
        delta_cst = delta
    else:
        raise NotImplementedError
    losses_with_name.append((delta, 'delta'))

    if bound_type == 'J':
        bound = mise
    elif bound_type == 'max-renyi':
        if truncated_mise:
            const = return_abs_max * (np.sqrt(2) + 1 / 3) \
                * tf.sqrt(tf.log(1 / delta))
            exploration_bonus = const * tf.sqrt(renyi_bound_)
            bound = mise + exploration_bonus
        else:
            const = return_abs_max * tf.sqrt(1 / delta - 1)
            exploration_bonus = const * tf.sqrt(renyi_bound_)
            bound = mise + exploration_bonus
    else:
        raise NotImplementedError

    losses_with_name.append((bound, 'Bound'))

    # Infos
    losses, loss_names = map(list, zip(*losses_with_name))

    # TF functions
    set_parameters = U.SetFromFlat(var_list)
    get_parameters = U.GetFlat(var_list)
    set_parameters_old = U.SetFromFlat(var_list_old)

    compute_behav = U.function(
        [actor_params_, disc_ret_, n_, mask_iters_],
        behavioral_log_pdf)
    compute_renyi = U.function(
        [], renyi_component)
    compute_bound = U.function(
        [actor_params_, disc_ret_, ret_, n_,
         mask_iters_, den_mise_log_, renyi_bound_], bound)
    compute_grad = U.function(
        [actor_params_, disc_ret_, ret_, n_,
         mask_iters_, den_mise_log_, renyi_bound_],
        U.flatgrad(bound, var_list))
    compute_losses = U.function(
        [actor_params_, disc_ret_, ret_, n_,
         mask_iters_, den_mise_log_, renyi_bound_], losses)
    compute_roba = U.function(
        [actor_params_, disc_ret_, ret_, n_,
         mask_iters_, den_mise_log_, renyi_bound_], [mise, exploration_bonus])

    # Set line search
    if line_search is not None:
        s = str(line_search)
        if s == "parabola" or s == "parabolic":
            print('Step size optimization through parabolic line search.')
            line_search = line_search_parabola
        else:
            raise ValueError("{} l.s. not implemented".format(line_search))

    # Tf initialization
    U.initialize()

    # Store behaviorals' params and their trajectories
    old_rhos_list = []
    all_eps = {}
    all_eps['actor_params'] = np.zeros(shape=[max_iters, pi._n_actor_weights])
    all_eps['disc_ret'] = np.zeros(max_iters)
    all_eps['ret'] = np.zeros(max_iters)
    mask_iters = np.zeros(max_iters)
    # Learning loop
    timesteps_so_far = 0
    iters_so_far = 0
    tstart = time.time()
    rho = get_parameters()
    theta = pi.resample()
    all_eps['actor_params'][iters_so_far, :] = theta
    print('Optimization of the %s bound' % (bound))
    while True:
        iters_so_far += 1
        mask_iters[:iters_so_far] = 1

        # Render one episode
        if render_after is not None and iters_so_far % render_after == 0:
            if hasattr(env, 'render'):
                render(env, pi, horizon)

        # Exit loop in the end
        if iters_so_far >= max_iters:
            print('Finished...')
            break

        # Learning iteration
        logger.log('********** Iteration %i ************' % iters_so_far)

        # Generate one trajectory
        with timed('sampling'):
            # Sample a trajectory with the newly parametrized actor
            ret, disc_ret, ep_len = eval_trajectory(
                env, pi, gamma, horizon, feature_fun)
            all_eps['ret'][iters_so_far-1] = ret
            all_eps['disc_ret'][iters_so_far-1] = disc_ret
            timesteps_so_far += ep_len
            # seg = sampler.collect(rho)

        # Store the parameters of the behavioral hyperpolicy
        old_rhos_list.append(rho)

        with timed('summaries before'):
            logger.record_tabular("Iteration", iters_so_far)
            logger.record_tabular("TimestepsSoFar", timesteps_so_far)
            logger.record_tabular("TimeElapsed", time.time() - tstart)

        # Save policy parameters to disk
        if save_weights:
            logger.record_tabular('Weights', str(get_parameters()))
            import pickle
            file = open('checkpoint.pkl', 'wb')
            pickle.dump(rho, file)

        # Tensor evaluations

        def evaluate_behav():
            args_behav = all_eps['actor_params'], all_eps['disc_ret'], \
                iters_so_far, mask_iters
            return compute_behav(*args_behav)

        def evaluate_renyi_component():
            return compute_renyi()

        args = all_eps['actor_params'], all_eps['disc_ret'], \
            all_eps['ret'], iters_so_far, mask_iters

        def evaluate_bound(den_mise_log, renyi_bound):
            args_bound = args + (den_mise_log, renyi_bound, )
            return compute_bound(*args_bound)

        def evaluate_grad(den_mise_log, renyi_bound):
            args_grad = args + (den_mise_log, renyi_bound, )
            return compute_grad(*args_grad)

        def evaluate_roba(den_mise_log, renyi_bound):
            args_roba = args + (den_mise_log, renyi_bound, )
            return compute_roba(*args_roba)

        if bound_type == 'J':
            evaluate_renyi = None
        elif bound_type == 'max-renyi':
            evaluate_renyi = evaluate_renyi_component
        else:
            raise NotImplementedError

        with timed("Optimization"):
            if multiple_init:
                bound = improvement = 0
                check = False
                for i in range(multiple_init):
                    rho_init = [np.arctanh(np.random.uniform(
                        pi.min_mean, pi.max_mean))]
                    rho_i, improvement_i, den_mise_log_i, bound_i = \
                        optimize_offline(evaluate_roba, pi,
                                         rho_init, drho,
                                         old_rhos_list,
                                         iters_so_far,
                                         mask_iters, set_parameters,
                                         set_parameters_old,
                                         evaluate_behav, evaluate_renyi,
                                         evaluate_bound,
                                         evaluate_grad, line_search,
                                         max_offline_ite=max_offline_iters)
                    if bound_i > bound:
                        check = True
                        rho = rho_i
                        improvement = improvement_i
                        den_mise_log = den_mise_log_i
                if not check:
                    den_mise_log = den_mise_log_i
            elif grid_optimization:
                rho, improvement, den_mise_log, renyi_bound = \
                    best_of_grid(pi, grid_optimization,
                                 rho, old_rhos_list,
                                 iters_so_far, mask_iters,
                                 set_parameters, set_parameters_old,
                                 delta_cst,
                                 evaluate_behav, evaluate_bound,
                                 evaluate_renyi, evaluate_roba,
                                 filename)
            else:
                rho, improvement, den_mise_log, renyi_bound, bound = \
                    optimize_offline(evaluate_roba, pi,
                                     rho, drho,
                                     old_rhos_list,
                                     iters_so_far,
                                     mask_iters, set_parameters,
                                     set_parameters_old,
                                     evaluate_behav, evaluate_renyi,
                                     evaluate_bound, evaluate_grad,
                                     line_search,
                                     max_offline_ite=max_offline_iters)
            set_parameters(rho)

        with timed('summaries after'):
            # Sample actor's parameters from hyperpolicy and assign to actor
            theta = pi.resample()
            all_eps['actor_params'][iters_so_far, :] = theta

            if env.spec.id == 'LQG1D-v0':
                mu1_actor = pi.eval_actor_mean([[1]])[0][0]
                mu1_higher = pi.eval_higher_mean([[1]])[0]
                sigma = pi.eval_higher_std()[0]
                logger.record_tabular("LQGmu1_actor", mu1_actor)
                logger.record_tabular("LQGmu1_higher", mu1_higher)
                logger.record_tabular("LQGsigma_higher", sigma)
            args_losses = args + (den_mise_log, renyi_bound, )
            meanlosses = np.array(compute_losses(*args_losses))
            for (lossname, lossval) in zip(loss_names, meanlosses):
                logger.record_tabular(lossname, lossval)

        # Print all info in a table
        logger.dump_tabular()

    # Close environment in the end
    env.close()
