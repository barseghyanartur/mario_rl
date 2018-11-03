import gym
import os
import random
from itertools import chain

import numpy as np

import torch.nn.functional as F
import torch.nn as nn
import torch
import cv2
import time
import datetime

from model import *

import torch.optim as optim
from torch.multiprocessing import Pipe, Process

from collections import deque

from tensorboardX import SummaryWriter
import gym_super_mario_bros
from nes_py.wrappers import BinarySpaceToDiscreteSpaceEnv
from gym_super_mario_bros.actions import SIMPLE_MOVEMENT, COMPLEX_MOVEMENT


class MarioEnvironment(Process):
    def __init__(
            self,
            env_id,
            is_render,
            env_idx,
            child_conn,
            history_size=4,
            h=84,
            w=84):
        super(MarioEnvironment, self).__init__()
        self.daemon = True
        self.env = BinarySpaceToDiscreteSpaceEnv(
            gym_super_mario_bros.make(env_id), movement)

        self.is_render = is_render
        self.env_idx = env_idx
        self.steps = 0
        self.episode = 0
        self.rall = 0
        self.recent_rlist = deque(maxlen=100)
        self.child_conn = child_conn

        self.history_size = history_size
        self.history = np.zeros([history_size, h, w])
        self.h = h
        self.w = w

        self.reset()

    def run(self):
        super(MarioEnvironment, self).run()
        while True:
            action = self.child_conn.recv()
            if self.is_render:
                self.env.render()
            obs, reward, done, info = self.env.step(action)

            if life_done:
                # when Mario loses life, changes the state to the terminal
                # state.
                if self.lives > info['life'] and info['life'] > 0:
                    force_done = True
                    self.lives = info['life']
                else:
                    force_done = done
                    self.lives = info['life']
            else:
                # normal terminal state
                force_done = done

            # reward range -15 ~ 15
            log_reward = reward / 15
            self.rall += log_reward

            if use_icm:
                if info['flag_get'] or self.stage < info['stage']:
                    r = 0.
                    self.stage = info['stage']
                # elif force_done:
                #     r = -1.
                else:
                    # reward
                    if self.max_pos < info['x_pos']:
                        self.max_pos = info['x_pos']
                        if self.max_pos % 100 == 0:
                            r = 1
                    if force_done:
                        self.max_pos = 0
                    r = 0.

            else:
                if info['flag_get'] or self.stage < info['stage']:
                    r = 5.
                    self.stage = info['stage']

                #r = 0
                # if self.max_pos < info['x_pos']:
                #    self.max_pos = info['x_pos']
                #    if self.max_pos % 100 == 0:
                #        r = 1.
                # if force_done:
                #    self.max_pos = 0
                r = log_reward
                if force_done:
                    r = -10

            self.history[:3, :, :] = self.history[1:, :, :]
            self.history[3, :, :] = self.pre_proc(obs)

            self.steps += 1

            if done:
                self.recent_rlist.append(self.rall)
                print(
                    "[Episode {}({})] Step: {}  Reward: {}  Recent Reward: {}  Stage: {} current x:{}   max x:{}".format(
                        self.episode,
                        self.env_idx,
                        self.steps,
                        self.rall,
                        np.mean(
                            self.recent_rlist),
                        info['stage'],
                        info['x_pos'],
                        self.max_pos))

                self.history = self.reset()
            if use_icm:
                self.child_conn.send(
                    [self.history[:, :, :], r, False, done, log_reward])
            else:
                self.child_conn.send(
                    [self.history[:, :, :], r, False, done, log_reward])

    def reset(self):
        self.steps = 0
        self.episode += 1
        self.rall = 0
        self.lives = 3
        self.stage = 1
        self.max_pos = 0
        self.get_init_state(self.env.reset())
        return self.history[:, :, :]

    def pre_proc(self, X):
        # grayscaling
        x = cv2.cvtColor(X, cv2.COLOR_RGB2GRAY)
        # resize
        x = cv2.resize(x, (self.h, self.w))
        x = np.float32(x) * (1.0 / 255.0)

        return x

    def get_init_state(self, s):
        for i in range(self.history_size):
            self.history[i, :, :] = self.pre_proc(s)


class ActorAgent(object):
    def __init__(
            self,
            input_size,
            output_size,
            num_env,
            num_step,
            gamma,
            lam=0.95,
            use_gae=True,
            use_cuda=False,
            use_noisy_net=True):
        self.model = CnnActorCriticNetwork(
            input_size, output_size, use_noisy_net)
        if use_icm:
            self.icm = CuriosityModel(input_size, output_size)
        self.num_env = num_env
        self.output_size = output_size
        self.input_size = input_size
        self.num_step = num_step
        self.gamma = gamma
        self.lam = lam
        self.use_gae = use_gae
        if use_icm:
            self.optimizer = optim.Adam(
                list(
                    self.model.parameters()) +
                list(
                    self.icm.parameters()),
                lr=learning_rate)
        else:
            self.optimizer = optim.Adam(
                self.model.parameters(), lr=learning_rate)

        self.device = torch.device('cuda' if use_cuda else 'cpu')

        self.model = self.model.to(self.device)
        if use_icm:
            self.icm = self.icm.to(self.device)

    def get_action(self, state):
        state = torch.Tensor(state).to(self.device)
        state = state.float()
        policy, value = self.model(state)
        policy = F.softmax(policy, dim=-1).data.cpu().numpy()

        action = self.random_choice_prob_index(policy)

        return action

    def compute_intrinsic_reward(self, state, next_state, action):
        state = torch.FloatTensor(state).to(self.device)
        next_state = torch.FloatTensor(next_state).to(self.device)
        action = torch.LongTensor(action).to(self.device)

        action_onehot = torch.FloatTensor(
            len(action), self.output_size).to(
            self.device)
        action_onehot.zero_()
        action_onehot.scatter_(1, action.view(len(action), -1), 1)

        real_next_state_feature, pred_next_state_feature, pred_action = self.icm(
            [state, next_state, action_onehot])
        intrinsic_reward = eta * \
            (real_next_state_feature - pred_next_state_feature).pow(2).sum(1) / 2
        # return np.clip(intrinsic_reward.data.cpu().numpy(), 0, 1)
        return intrinsic_reward.data.cpu().numpy()

    @staticmethod
    def random_choice_prob_index(p, axis=1):
        r = np.expand_dims(np.random.rand(p.shape[1 - axis]), axis=axis)
        return (p.cumsum(axis=axis) > r).argmax(axis=axis)

    def forward_transition(self, state, next_state):
        state = torch.from_numpy(state).to(self.device)
        state = state.float()
        policy, value = agent.model(state)

        next_state = torch.from_numpy(next_state).to(self.device)
        next_state = next_state.float()
        _, next_value = agent.model(next_state)

        value = value.data.cpu().numpy().squeeze()
        next_value = next_value.data.cpu().numpy().squeeze()

        return value, next_value, policy

    def train_model(
            self,
            s_batch,
            next_s_batch,
            target_batch,
            y_batch,
            adv_batch):
        s_batch = torch.FloatTensor(s_batch).to(self.device)
        next_s_batch = torch.FloatTensor(next_s_batch).to(self.device)
        target_batch = torch.FloatTensor(target_batch).to(self.device)
        y_batch = torch.LongTensor(y_batch).to(self.device)
        adv_batch = torch.FloatTensor(adv_batch).to(self.device)

        sample_range = np.arange(len(s_batch))
        ce = nn.CrossEntropyLoss()
        forward_mse = nn.MSELoss()
        self.model.train()
        if use_icm:
            self.icm.train()

        with torch.no_grad():
            # for multiply advantage
            policy_old, value_old = self.model(s_batch)
            m_old = Categorical(F.softmax(policy_old, dim=-1))
            log_prob_old = m_old.log_prob(y_batch)

        for i in range(epoch):
            np.random.shuffle(sample_range)
            for j in range(int(len(s_batch) / batch_size)):
                sample_idx = sample_range[batch_size * j:batch_size * (j + 1)]

                # --------------------------------------------------------------------------------
                if use_icm:
                    # for Curiosity-driven
                    action_onehot = torch.FloatTensor(
                        len(s_batch[sample_idx]), self.output_size).to(self.device)
                    action_onehot.zero_()
                    action_onehot.scatter_(1, y_batch.view(
                        len(y_batch[sample_idx]), -1), 1)

                    real_next_state_feature, pred_next_state_feature, pred_action = self.icm(
                        [s_batch[sample_idx], next_s_batch[sample_idx], action_onehot])
                    # print(pred_action)
                    inverse_loss = ce(
                        pred_action, y_batch[sample_idx].detach())
                    forward_loss = forward_mse(
                        pred_next_state_feature, real_next_state_feature.detach())
                # ---------------------------------------------------------------------------------

                policy, value = self.model(s_batch[sample_idx])
                m = Categorical(F.softmax(policy, dim=-1))
                log_prob = m.log_prob(y_batch[sample_idx])

                ratio = torch.exp(log_prob - log_prob_old[sample_idx])

                surr1 = ratio * adv_batch[sample_idx]
                surr2 = torch.clamp(
                    ratio,
                    1.0 - ppo_eps,
                    1.0 + ppo_eps) * adv_batch[sample_idx]

                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = F.mse_loss(
                    value.sum(1), target_batch[sample_idx])
                entropy = m.entropy().mean()

                self.optimizer.zero_grad()
                if use_icm:
                    loss = (actor_loss + 0.5 * critic_loss) + icm_scale * \
                        ((1 - beta) * inverse_loss + beta * forward_loss)
                else:
                    loss = actor_loss + 0.5 * critic_loss - entropy_coef * entropy
                loss.backward()
                if use_icm:
                    torch.nn.utils.clip_grad_norm_(
                        list(
                            self.model.parameters()) +
                        list(
                            self.icm.parameters()),
                        clip_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(
                        self.model.parameters(), clip_grad_norm)
                self.optimizer.step()


def make_train_data(reward, done, value, next_value):
    discounted_return = np.empty([num_step])

    # Discounted Return
    if use_gae:
        gae = 0
        for t in range(num_step - 1, -1, -1):
            delta = reward[t] + gamma * \
                next_value[t] * (1 - done[t]) - value[t]
            gae = delta + gamma * lam * (1 - done[t]) * gae

            discounted_return[t] = gae + value[t]

        # For Actor
        adv = discounted_return - value

    else:
        for t in range(num_step - 1, -1, -1):
            running_add = reward[t] + gamma * next_value[t] * (1 - done[t])
            discounted_return[t] = running_add

        # For Actor
        adv = discounted_return - value

    return discounted_return, adv


class RunningMeanStd(object):
    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Parallel_algorithm
    def __init__(self, epsilon=1e-4, shape=()):
        self.mean = np.zeros(shape, 'float64')
        self.var = np.ones(shape, 'float64')
        self.count = epsilon

    def update(self, x):
        batch_mean = np.mean(x, axis=0)
        batch_var = np.var(x, axis=0)
        batch_count = x.shape[0]
        self.update_from_moments(batch_mean, batch_var, batch_count)

    def update_from_moments(self, batch_mean, batch_var, batch_count):
        delta = batch_mean - self.mean
        tot_count = self.count + batch_count

        new_mean = self.mean + delta * batch_count / tot_count
        m_a = self.var * (self.count)
        m_b = batch_var * (batch_count)
        M2 = m_a + m_b + np.square(delta) * self.count * \
            batch_count / (self.count + batch_count)
        new_var = M2 / (self.count + batch_count)

        new_count = batch_count + self.count

        self.mean = new_mean
        self.var = new_var
        self.count = new_count


class RewardForwardFilter(object):
    def __init__(self, gamma):
        self.rewems = None
        self.gamma = gamma

    def update(self, rews):
        if self.rewems is None:
            self.rewems = rews
        else:
            self.rewems = self.rewems * self.gamma + rews
        return self.rewems


if __name__ == '__main__':
    env_id = 'SuperMarioBros-v0'
    movement = COMPLEX_MOVEMENT
    env = BinarySpaceToDiscreteSpaceEnv(
        gym_super_mario_bros.make(env_id), movement)
    input_size = env.observation_space.shape  # 4
    output_size = env.action_space.n  # 2

    env.close()

    writer = SummaryWriter()
    use_cuda = True
    use_gae = True
    life_done = True

    is_load_model = False
    is_training = True

    is_render = False
    use_standardization = True
    use_noisy_net = False
    use_icm = True

    model_path = 'models/{}_{}.model'.format(env_id,
                                             datetime.date.today().isoformat())
    load_model_path = 'models/SuperMarioBros-v0_2018-09-26.model'

    lam = 0.95
    num_worker = 16
    num_step = 128
    ppo_eps = 0.1
    epoch = 3
    batch_size = 256
    max_step = 1.15e8

    learning_rate = 0.0001
    lr_schedule = False

    stable_eps = 1e-30
    entropy_coef = 0.02
    alpha = 0.99
    gamma = 0.99
    clip_grad_norm = 0.5

    # Curiosity param
    icm_scale = 10.0
    beta = 0.2
    eta = 1.0
    reward_scale = 1

    agent = ActorAgent(
        input_size,
        output_size,
        num_worker,
        num_step,
        gamma,
        use_cuda=use_cuda,
        use_noisy_net=use_noisy_net)
    reward_rms = RunningMeanStd()
    discounted_reward = RewardForwardFilter(gamma)

    if is_load_model:
        if use_cuda:
            agent.model.load_state_dict(torch.load(load_model_path))
        else:
            agent.model.load_state_dict(
                torch.load(
                    load_model_path,
                    map_location='cpu'))

    if not is_training:
        agent.model.eval()

    works = []
    parent_conns = []
    child_conns = []
    for idx in range(num_worker):
        parent_conn, child_conn = Pipe()
        work = MarioEnvironment(env_id, is_render, idx, child_conn)
        work.start()
        works.append(work)
        parent_conns.append(parent_conn)
        child_conns.append(child_conn)

    states = np.zeros([num_worker, 4, 84, 84])

    sample_episode = 0
    sample_rall = 0
    sample_i_rall = 0
    sample_step = 0
    sample_env_idx = 0
    global_step = 0
    recent_prob = deque(maxlen=10)

    while True:
        total_state, total_reward, total_done, total_next_state, total_action = [], [], [], [], []
        global_step += (num_worker * num_step)

        for _ in range(num_step):
            if not is_training:
                time.sleep(0.05)

            agent.model.eval()

            if use_icm:
                agent.icm.eval()

            actions = agent.get_action(states)

            for parent_conn, action in zip(parent_conns, actions):
                parent_conn.send(action)

            next_states, rewards, dones, real_dones, log_rewards = [], [], [], [], []
            for parent_conn in parent_conns:
                s, r, d, rd, lr = parent_conn.recv()
                next_states.append(s)
                rewards.append(r)
                dones.append(d)
                real_dones.append(rd)
                log_rewards.append(lr)

            next_states = np.stack(next_states)
            rewards = np.hstack(rewards) * reward_scale
            dones = np.hstack(dones)
            real_dones = np.hstack(real_dones)

            if use_icm:
                intrinsic_reward = agent.compute_intrinsic_reward(
                    states, next_states, actions)
                rewards += intrinsic_reward

            total_state.append(states)
            total_next_state.append(next_states)
            total_reward.append(rewards)
            total_done.append(dones)
            total_action.append(actions)

            states = next_states[:, :, :, :]

            sample_rall += log_rewards[sample_env_idx]
            if use_icm:
                sample_i_rall += intrinsic_reward[sample_env_idx]
            sample_step += 1
            if real_dones[sample_env_idx]:
                sample_episode += 1
                writer.add_scalar('data/reward', sample_rall, sample_episode)
                if use_icm:
                    writer.add_scalar(
                        'data/i-reward', sample_i_rall, sample_episode)
                writer.add_scalar('data/step', sample_step, sample_episode)
                sample_rall = 0
                sample_i_rall = 0
                sample_step = 0

        if is_training:
            total_state = np.stack(total_state).transpose(
                [1, 0, 2, 3, 4]).reshape([-1, 4, 84, 84])
            total_next_state = np.stack(total_next_state).transpose(
                [1, 0, 2, 3, 4]).reshape([-1, 4, 84, 84])
            total_reward = np.stack(total_reward).transpose().reshape([-1])
            total_action = np.stack(total_action).transpose().reshape([-1])
            total_done = np.stack(total_done).transpose().reshape([-1])

            value, next_value, policy = agent.forward_transition(
                total_state, total_next_state)
            if use_icm:
                total_reward_per_env = np.array([discounted_reward.update(
                    reward_per_step) for reward_per_step in total_reward.reshape([num_worker, -1]).T])
                total_reawrd_per_env = total_reward_per_env.reshape([-1])
                mean, std, count = np.mean(total_reward), np.std(
                    total_reward), len(total_reward)
                reward_rms.update_from_moments(mean, std ** 2, count)
                total_reward /= np.sqrt(reward_rms.var)

            # logging utput to see how convergent it is.
            policy = policy.detach()
            m = F.softmax(policy, dim=-1)
            recent_prob.append(m.max(1)[0].mean().cpu().numpy())
            writer.add_scalar(
                'data/max_prob',
                np.mean(recent_prob),
                sample_episode)

            total_target = []
            total_adv = []
            for idx in range(num_worker):
                target, adv = make_train_data(total_reward[idx * num_step:(idx + 1) * num_step],
                                              total_done[idx *
                                                         num_step:(idx + 1) * num_step],
                                              value[idx *
                                                    num_step:(idx + 1) * num_step],
                                              next_value[idx * num_step:(idx + 1) * num_step])
                total_target.append(target)
                total_adv.append(adv)

            if use_standardization:
                adv = (adv - adv.mean()) / (adv.std() + stable_eps)

            agent.train_model(
                total_state,
                total_next_state,
                np.hstack(total_target),
                total_action,
                np.hstack(total_adv))

            # adjust learning rate
            if lr_schedule:
                new_learing_rate = learning_rate - \
                    (global_step / max_step) * learning_rate
                for param_group in agent.optimizer.param_groups:
                    param_group['lr'] = new_learing_rate
                    writer.add_scalar(
                        'data/lr', new_learing_rate, sample_episode)

            if global_step % (num_worker * num_step * 100) == 0:
                torch.save(agent.model.state_dict(), model_path)