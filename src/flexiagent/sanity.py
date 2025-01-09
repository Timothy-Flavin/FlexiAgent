import gymnasium as gym
import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm, trange
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable
from torch.distributions import Categorical
from Agent import MixedActor

env = gym.make("CartPole-v1")
torch.manual_seed(1)
learning_rate = 0.01
gamma = 0.99


class Policy(nn.Module):
    def __init__(self):
        super(Policy, self).__init__()
        self.state_space = env.observation_space.shape[0]
        self.action_space = env.action_space.n

        # self.l1 = nn.Linear(self.state_space, 128, bias=False)
        # self.l2 = nn.Linear(128, self.action_space, bias=False)

        self.gamma = gamma

        # Episode policy and reward history
        self.policy_history = Variable(torch.Tensor())
        self.reward_episode = []
        # Overall reward and loss history
        self.reward_history = []
        self.loss_history = []

        self.model = MixedActor(
            obs_dim=self.state_space,
            continuous_action_dim=2,
            min_actions=np.array([-1.0, -1.0]),
            max_actions=np.array([1.0, 1.0]),
            discrete_action_dims=[env.action_space.n],
            hidden_dims=[128],
        )

    def forward(self, x):
        # model = torch.nn.Sequential(
        #    self.l1, nn.Dropout(p=0.6), nn.ReLU(), self.l2, nn.Softmax(dim=-1)
        # )
        ca, da = self.model(x)
        # print(ca, da)
        return da[0]


policy = Policy()
optimizer = optim.Adam(policy.parameters(), lr=learning_rate)


def select_action(state):
    # Select an action (0 or 1) by running policy model and choosing based on the probabilities in state
    state = torch.from_numpy(state).type(torch.FloatTensor)
    state = policy(Variable(state))
    c = Categorical(state)
    action = c.sample()

    # Add log probability of our chosen action to our history
    # print(policy.policy_history)
    # print(policy.policy_history.shape)
    if policy.policy_history.shape[0] != 0:
        # print(policy.policy_history, c.log_prob(action))
        policy.policy_history = torch.cat(
            [policy.policy_history, c.log_prob(action).unsqueeze(-1)]
        )
    else:
        policy.policy_history = c.log_prob(action).unsqueeze(-1)
    return action


def update_policy():
    R = 0
    rewards = []

    # Discount future rewards back to the present using gamma
    for r in policy.reward_episode[::-1]:
        R = r + policy.gamma * R
        rewards.insert(0, R)
    # print(rewards)
    # Scale rewards
    rewards = torch.FloatTensor(rewards)
    rewards = (rewards - rewards.mean()) / (rewards.std() + np.finfo(np.float32).eps)
    # print(rewards)
    # input()
    # Calculate loss
    loss = torch.sum(torch.mul(policy.policy_history, Variable(rewards)).mul(-1), -1)

    # Update network weights
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

    # Save and intialize episode history counters
    policy.loss_history.append(loss.item())
    policy.reward_history.append(np.sum(policy.reward_episode))
    policy.policy_history = Variable(torch.Tensor())
    policy.reward_episode = []


def main(episodes):
    running_reward = 10
    for episode in range(episodes):
        state, info = env.reset()  # Reset environment and record the starting state
        done = False

        for time in range(1000):
            action = select_action(state)
            # Step through environment using chosen action
            state, reward, term, done, _ = env.step(action.item())

            # Save reward
            policy.reward_episode.append(reward)
            if done or term:
                break

        # Used to determine when the environment is solved.
        running_reward = (running_reward * 0.99) + (time * 0.01)

        update_policy()

        if episode % 50 == 0:
            print(
                "Episode {}\tLast length: {:5d}\tAverage length: {:.2f}".format(
                    episode, time, running_reward
                )
            )

        if running_reward > env.spec.reward_threshold:
            print(
                "Solved! Running reward is now {} and the last episode runs to {} time steps!".format(
                    running_reward, time
                )
            )
            break


episodes = 1000
main(episodes)
window = int(episodes / 20)

fig, ((ax1), (ax2)) = plt.subplots(2, 1, sharey=True, figsize=[9, 9])
rolling_mean = pd.Series(policy.reward_history).rolling(window).mean()
std = pd.Series(policy.reward_history).rolling(window).std()
ax1.plot(rolling_mean)
ax1.fill_between(
    range(len(policy.reward_history)),
    rolling_mean - std,
    rolling_mean + std,
    color="orange",
    alpha=0.2,
)
ax1.set_title("Episode Length Moving Average ({}-episode window)".format(window))
ax1.set_xlabel("Episode")
ax1.set_ylabel("Episode Length")

ax2.plot(policy.reward_history)
ax2.set_title("Episode Length")
ax2.set_xlabel("Episode")
ax2.set_ylabel("Episode Length")

fig.tight_layout(pad=2)
plt.show()
