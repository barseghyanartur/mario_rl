import gym
import cv2

from abc import abstractmethod
from collections import deque

import gym_super_mario_bros
from nes_py.wrappers import BinarySpaceToDiscreteSpaceEnv
from gym_super_mario_bros.actions import SIMPLE_MOVEMENT, COMPLEX_MOVEMENT

from torch.multiprocessing import Pipe, Process

from model import *
from config import *

train_method = default_config['TrainMethod']


class Environment(Process):
    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def reset(self):
        pass

    @abstractmethod
    def pre_proc(self, x):
        pass

    @abstractmethod
    def get_init_state(self, x):
        pass


class AtariEnvironment(Environment):
    def __init__(
            self,
            env_id,
            is_render,
            env_idx,
            child_conn,
            history_size=4,
            h=84,
            w=84):
        super(AtariEnvironment, self).__init__()
        self.daemon = True
        self.env = gym.make(env_id)
        self.env_id = env_id
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
        self.lives = self.env.env.ale.lives()

    def run(self):
        super(AtariEnvironment, self).run()
        while True:
            action = self.child_conn.recv()
            if self.is_render:
                self.env.render()

            if 'Breakout' in self.env_id:
                action += 1

            _, reward, done, info = self.env.step(action)
            log_reward = reward
            if self.lives > info['ale.lives'] and info['ale.lives'] > 0:
                force_done = True
                self.lives = info['ale.lives']
            else:
                force_done = done

            self.history[:3, :, :] = self.history[1:, :, :]
            self.history[3, :, :] = self.pre_proc(
                self.env.env.ale.getScreenGrayscale().squeeze().astype('float32'))

            self.rall += reward
            self.steps += 1

            if train_method == 'ICM':
                reward = 0.

            if train_method == 'RND':
                reward = 0.

            if done:
                self.recent_rlist.append(self.rall)
                print("[Episode {}({})] Step: {}  Reward: {}  Recent Reward: {}".format(
                    self.episode, self.env_idx, self.steps, self.rall, np.mean(self.recent_rlist)))

                self.history = self.reset()

            self.child_conn.send(
                [self.history[:, :, :], reward, force_done, done, log_reward])

    def reset(self):
        self.steps = 0
        self.episode += 1
        self.rall = 0
        self.env.reset()
        self.lives = self.env.env.ale.lives()
        self.get_init_state(
            self.env.env.ale.getScreenGrayscale().squeeze().astype('float32'))
        return self.history[:, :, :]

    def pre_proc(self, X):
        x = cv2.resize(X, (self.h, self.w))
        x *= (1.0 / 255.0)

        return x

    def get_init_state(self, s):
        for i in range(self.history_size):
            self.history[i, :, :] = self.pre_proc(s)


class MarioEnvironment(Process):
    def __init__(
            self,
            env_id,
            is_render,
            env_idx,
            child_conn,
            history_size=4,
            h=84,
            w=84, movement=COMPLEX_MOVEMENT):
        super(MarioEnvironment, self).__init__()
        self.daemon = True
        self.env = BinarySpaceToDiscreteSpaceEnv(
            gym_super_mario_bros.make(env_id), COMPLEX_MOVEMENT)

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

            # when Mario loses life, changes the state to the terminal
            # state.
            if self.lives > info['life'] and info['life'] > 0:
                force_done = True
                self.lives = info['life']
            else:
                force_done = done
                self.lives = info['life']

            # reward range -15 ~ 15
            log_reward = reward / 15
            self.rall += log_reward

            r = log_reward

            # ext reward = 0
            if train_method == 'ICM':
                r = 0.

            if train_method == 'RND':
                reward = 0.

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
            else:
                if default_config['TrainMethod'] == 'ICM':
                    self.child_conn.send([self.history[:, :, :], r, False, done, log_reward])
                else:
                    self.child_conn.send([self.history[:, :, :], r, force_done, done, log_reward])

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
