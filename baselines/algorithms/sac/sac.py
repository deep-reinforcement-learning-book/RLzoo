'''
Soft Actor-Critic
using target Q instead of V net: 2 Q net, 2 target Q net, 1 policy net
adding alpha loss

paper: https://arxiv.org/pdf/1812.05905.pdf
Actor policy is stochastic.

Env: Openai Gym Pendulum-v0, continuous action space

tensorflow 2.0.0a0
tensorflow-probability 0.6.0
tensorlayer 2.0.0

&&
pip install box2d box2d-kengz --user
'''

import argparse
import math
import random
import time

import matplotlib.pyplot as plt
import numpy as np
from IPython.display import clear_output

import gym
import tensorflow as tf
import tensorflow_probability as tfp
import tensorlayer as tl
from tensorlayer.layers import Dense
from tensorlayer.models import Model
from common.utils import *
from common.buffer import *
from common.value_networks import *
from common.policy_networks import *

tfd = tfp.distributions
Normal = tfd.Normal

tl.logging.set_verbosity(tl.logging.DEBUG)


class SAC():
    ''' Soft Actor-Critic '''
    def __init__(self, net_list, state_dim, action_dim, replay_buffer_capacity=5e5, action_range=1., hidden_dim=32, num_hidden_layer=3, soft_q_lr=3e-4, policy_lr=3e-4, alpha_lr=3e-4):
        self.replay_buffer = ReplayBuffer(replay_buffer_capacity)
        self.action_dim = action_dim
        self.action_range = action_range

        # get all networks
        [self.soft_q_net1, self.soft_q_net2, self.target_soft_q_net1, self.target_soft_q_net2, self.policy_net]=net_list
       
        self.log_alpha = tf.Variable(0, dtype=np.float32, name='log_alpha')
        self.alpha = tf.math.exp(self.log_alpha)
        print('Soft Q Network (1,2): ', self.soft_q_net1)
        print('Policy Network: ', self.policy_net)

        # initialize weights of target networks
        self.target_soft_q_net1 = self.target_ini(self.soft_q_net1, self.target_soft_q_net1)
        self.target_soft_q_net2 = self.target_ini(self.soft_q_net2, self.target_soft_q_net2)

        self.soft_q_optimizer1 = tf.optimizers.Adam(soft_q_lr)
        self.soft_q_optimizer2 = tf.optimizers.Adam(soft_q_lr)
        self.policy_optimizer = tf.optimizers.Adam(policy_lr)
        self.alpha_optimizer = tf.optimizers.Adam(alpha_lr)
    
    def evaluate(self, state, epsilon=1e-6):
        ''' generate action with state for calculating gradients '''
        state = state.astype(np.float32)
        mean, log_std = self.policy_net(state)
        std = tf.math.exp(log_std)  # no clip in evaluation, clip affects gradients flow

        normal = Normal(0, 1)
        z = normal.sample()
        action_0 = tf.math.tanh(mean + std * z)  # TanhNormal distribution as actions; reparameterization trick
        action = self.action_range * action_0
        # according to original paper, with an extra last term for normalizing different action range
        log_prob = Normal(mean, std).log_prob(mean + std * z) - tf.math.log(1. - action_0**2 +
                                                                            epsilon) - np.log(self.action_range)
        # both dims of normal.log_prob and -log(1-a**2) are (N,dim_of_action);
        # the Normal.log_prob outputs the same dim of input features instead of 1 dim probability,
        # needs sum up across the dim of actions to get 1 dim probability; or else use Multivariate Normal.
        log_prob = tf.reduce_sum(log_prob, axis=1)[:, np.newaxis]  # expand dim as reduce_sum causes 1 dim reduced

        return action, log_prob, z, mean, log_std

    def get_action(self, state, deterministic=False):
        ''' generate action with state for interaction with envronment '''
        mean, log_std = self.policy_net(np.array([state.astype(np.float32)]))
        std = tf.math.exp(log_std)

        normal = Normal(0, 1)
        z = normal.sample()
        action = self.action_range * tf.math.tanh(
            mean + std * z
        )  # TanhNormal distribution as actions; reparameterization trick

        action = self.action_range * mean if deterministic else action
        return action.numpy()[0]

    def sample_action(self, ):
        ''' generate random actions for exploration '''
        a = tf.random.uniform([self.action_dim], -self.action_range, self.action_range)

        return self.action_range * a.numpy()

    def target_ini(self, net, target_net):
        ''' hard-copy update for initializing target networks '''
        for target_param, param in zip(target_net.trainable_weights, net.trainable_weights):
            target_param.assign(param)
        return target_net

    def target_soft_update(self, net, target_net, soft_tau):
        ''' soft update the target net with Polyak averaging '''
        for target_param, param in zip(target_net.trainable_weights, net.trainable_weights):
            target_param.assign(  # copy weight value into target parameters
                target_param * (1.0 - soft_tau) + param * soft_tau
            )
        return target_net

    def update(self, batch_size, reward_scale=10., auto_entropy=True, target_entropy=-2, gamma=0.99, soft_tau=1e-2):
        ''' update all networks in SAC '''
        state, action, reward, next_state, done = self.replay_buffer.sample(batch_size)

        reward = reward[:, np.newaxis]  # expand dim
        done = done[:, np.newaxis]

        reward = reward_scale * (reward -
                                 np.mean(reward, axis=0)) / (np.std(reward, axis=0) + 1e-6)  # normalize with batch mean and std

        # Training Q Function
        new_next_action, next_log_prob, _, _, _ = self.evaluate(next_state)
        target_q_input = tf.concat([next_state, new_next_action], 1)  # the dim 0 is number of samples
        target_q_min = tf.minimum(
            self.target_soft_q_net1(target_q_input), self.target_soft_q_net2(target_q_input)
        ) - self.alpha * next_log_prob
        target_q_value = reward + (1 - done) * gamma * target_q_min  # if done==1, only reward
        q_input = tf.concat([state, action], 1)  # the dim 0 is number of samples

        with tf.GradientTape() as q1_tape:
            predicted_q_value1 = self.soft_q_net1(q_input)
            q_value_loss1 = tf.reduce_mean(tf.losses.mean_squared_error(predicted_q_value1, target_q_value))
        q1_grad = q1_tape.gradient(q_value_loss1, self.soft_q_net1.trainable_weights)
        self.soft_q_optimizer1.apply_gradients(zip(q1_grad, self.soft_q_net1.trainable_weights))

        with tf.GradientTape() as q2_tape:
            predicted_q_value2 = self.soft_q_net2(q_input)
            q_value_loss2 = tf.reduce_mean(tf.losses.mean_squared_error(predicted_q_value2, target_q_value))
        q2_grad = q2_tape.gradient(q_value_loss2, self.soft_q_net2.trainable_weights)
        self.soft_q_optimizer2.apply_gradients(zip(q2_grad, self.soft_q_net2.trainable_weights))

        # Training Policy Function
        with tf.GradientTape() as p_tape:
            new_action, log_prob, z, mean, log_std = self.evaluate(state)
            new_q_input = tf.concat([state, new_action], 1)  # the dim 0 is number of samples
            ''' implementation 1 '''
            predicted_new_q_value = tf.minimum(self.soft_q_net1(new_q_input), self.soft_q_net2(new_q_input))
            ''' implementation 2 '''
            # predicted_new_q_value = self.soft_q_net1(new_q_input)
            policy_loss = tf.reduce_mean(self.alpha * log_prob - predicted_new_q_value)
        p_grad = p_tape.gradient(policy_loss, self.policy_net.trainable_weights)
        self.policy_optimizer.apply_gradients(zip(p_grad, self.policy_net.trainable_weights))

        # Updating alpha w.r.t entropy
        # alpha: trade-off between exploration (max entropy) and exploitation (max Q)
        if auto_entropy is True:
            with tf.GradientTape() as alpha_tape:
                alpha_loss = -tf.reduce_mean((self.log_alpha * (log_prob + target_entropy)))
            alpha_grad = alpha_tape.gradient(alpha_loss, [self.log_alpha])
            self.alpha_optimizer.apply_gradients(zip(alpha_grad, [self.log_alpha]))
            self.alpha = tf.math.exp(self.log_alpha)
        else:  # fixed alpha
            self.alpha = 1.
            alpha_loss = 0

    # Soft update the target value nets
        self.target_soft_q_net1 = self.target_soft_update(self.soft_q_net1, self.target_soft_q_net1, soft_tau)
        self.target_soft_q_net2 = self.target_soft_update(self.soft_q_net2, self.target_soft_q_net2, soft_tau)

    def save_weights(self): 
        ''' save trained weights '''
        save_model(self.soft_q_net1, 'model_q_net1', 'SAC')
        save_model(self.soft_q_net2, 'model_q_net2', 'SAC')
        save_model(self.target_soft_q_net1, 'model_target_q_net1', 'SAC')
        save_model(self.target_soft_q_net2, 'model_target_q_net2', 'SAC')
        save_model(self.policy_net, 'model_policy_net', 'SAC')


    def load_weights(self):
        ''' load trained weights '''
        load_model(self.soft_q_net1, 'model_q_net1', 'SAC')
        load_model(self.soft_q_net2, 'model_q_net2', 'SAC')
        load_model(self.target_soft_q_net1, 'model_target_q_net1', 'SAC')
        load_model(self.target_soft_q_net2, 'model_target_q_net2', 'SAC')
        load_model(self.policy_net, 'model_policy_net', 'SAC')


    def learn(self, env, train_episodes, test_episodes=1000, max_steps=150, batch_size=64, explore_steps=500, \
        update_itr=3, policy_target_update_interval = 3,  reward_scale = 1. , seed=2, save_interval=20, \
        mode='train', AUTO_ENTROPY = True, DETERMINISTIC = False):
        '''
        parameters
        ----------
        env: learning environment
        train_episodes:  total number of episodes for training
        test_episodes:  total number of episodes for testing
        max_steps:  maximum number of steps for one episode
        batch_size:  udpate batchsize
        explore_steps:  for random action sampling in the beginning of training
        update_itr: repeated updates for single step
        policy_target_update_interval: delayed update for the policy network and target networks
        reward_scale: value range of reward
        seed: random seed
        save_interval: timesteps for saving the weights and plotting the results
        mode: 'train' or 'test'
        AUTO_ENTROPY: automatically udpating variable alpha for entropy
        DETERMINISTIC: stochastic action policy if False, otherwise deterministic

        '''
        np.random.seed(seed)
        tf.random.set_seed(seed)  # reproducible

        # training loop
        if mode=='train':
            frame_idx = 0
            rewards = []
            t0 = time.time()
            for eps in range(train_episodes):
                state = env.reset()
                state = state.astype(np.float32)
                episode_reward = 0

                for step in range(max_steps):
                    if frame_idx > explore_steps:
                        action = self.get_action(state, deterministic=DETERMINISTIC)
                    else:
                        action = self.sample_action()

                    next_state, reward, done, _ = env.step(action)
                    next_state = next_state.astype(np.float32)
                    env.render()
                    done = 1 if done ==True else 0

                    self.replay_buffer.push(state, action, reward, next_state, done)

                    state = next_state
                    episode_reward += reward
                    frame_idx += 1

                    if len(self.replay_buffer) > batch_size:
                        for i in range(update_itr):
                            self.update(
                                batch_size, reward_scale=reward_scale, auto_entropy=AUTO_ENTROPY,
                                target_entropy=-1. * self.action_dim
                            )

                    if done:
                        break
                if eps % int(save_interval) == 0:
                    plot_save_log(rewards, Algorithm_name='SAC', Env_name=env.spec.id)
                    self.save_weights()
                print('Episode: {}/{}  | Episode Reward: {:.4f}  | Running Time: {:.4f}'\
                .format(eps, train_episodes, episode_reward, time.time()-t0 ))
                rewards.append(episode_reward)
            self.save_weights()

        if mode=='test':
            frame_idx = 0
            rewards = []
            t0 = time.time()
            self.load_weights()
            #set test mode
            self.soft_q_net1.eval()
            self.soft_q_net2.eval()
            self.target_soft_q_net1.eval()
            self.target_soft_q_net2.eval()
            self.policy_net.eval()

            for eps in range(test_episodes):
                state = env.reset()
                state = state.astype(np.float32)
                episode_reward = 0

                for step in range(max_steps):
                    action = self.get_action(state, deterministic=DETERMINISTIC)
                    next_state, reward, done, _ = env.step(action)
                    next_state = next_state.astype(np.float32)
                    env.render()
                    done = 1 if done ==True else 0

                    state = next_state
                    episode_reward += reward
                    frame_idx += 1

                    # if frame_idx % 50 == 0:
                    #     plot(frame_idx, rewards)

                    if done:
                        break
                print('Episode: {}/{}  | Episode Reward: {:.4f}  | Running Time: {:.4f}'\
                .format(eps, test_episodes, episode_reward, time.time()-t0 ) )
                rewards.append(episode_reward)
