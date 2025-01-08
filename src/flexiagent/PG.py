from Agent import ValueS, MixedActor, Agent
import torch
from flexibuff import FlexiBatch
from torch.distributions import Categorical
from Util import T
import numpy as np


class PG(Agent):
    def __init__(
        self,
        obs_dim,
        continuous_action_dim=0,
        max_actions=None,
        min_actions=None,
        discrete_action_dims=None,
        lr=2.5e-4,
        gamma=0.99,
        n_epochs=2,
        device="cpu",
        entropy_loss=0.05,
        hidden_dims=[256, 256],
        activation="relu",
        ppo_clip=0.2,
        value_loss_coef=0.5,
        advantage_type="gae",
        norm_advantages=False,
        mini_batch_size=64,
    ):
        super().__init__()
        assert (
            continuous_action_dim > 0 or discrete_action_dims is not None
        ), "At least one action dim should be provided"
        self.ppo_clip = ppo_clip
        self.value_loss_coef = value_loss_coef
        self.mini_batch_size = mini_batch_size
        assert advantage_type.lower() in [
            "gae",
            "a2c",
            "constant",
            "gv",
            "g",
        ], "Invalid advantage type"
        self.advantage_type = advantage_type
        self.gae_lambda = 0.95
        self.device = device
        self.gamma = gamma
        self.obs_dim = obs_dim
        self.continuous_action_dim = continuous_action_dim
        self.discrete_action_dims = discrete_action_dims
        self.n_epochs = n_epochs
        self.activation = activation
        self.norm_advantages = norm_advantages

        self.policy_loss = 1
        self.critic_loss_coef = 1
        self.entropy_loss = entropy_loss

        self.actor = MixedActor(
            obs_dim=obs_dim,
            continuous_action_dim=continuous_action_dim,
            discrete_action_dims=discrete_action_dims,
            max_actions=max_actions,
            min_actions=min_actions,
            hidden_dims=hidden_dims,
            device=device,
            orthogonal_init=True,
            activation="tanh",
        )

        self.critic = ValueS(
            obs_dim=obs_dim,
            hidden_dim=256,
            device=self.device,
            orthogonal_init=True,
            activation="tanh",
        )
        self.actor_logstd = torch.nn.Parameter(
            torch.zeros(1, continuous_action_dim), requires_grad=True
        )
        self.total_params = self.parameters()
        self.optimizer = torch.optim.AdamW(self.total_params, lr=lr)
        # self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=lr_critic)

    def _sample_multi_discrete(
        self, logits, debug=False
    ):  # logits of the form [action_dim, batch_size, action_dim_size]
        actions = torch.zeros(
            size=(len(self.discrete_action_dims),),
            device=self.device,
            dtype=torch.int,
        )
        log_probs = torch.zeros(
            size=(len(self.discrete_action_dims),),
            device=self.device,
        )
        for i in range(len(self.discrete_action_dims)):
            dist = Categorical(logits=logits[i])
            actions[i] = dist.sample()
            log_probs[i] = dist.log_prob(actions[i])
        return actions, log_probs

    def train_actions(self, observations, action_mask=None, step=False, debug=False):

        if not torch.is_tensor(observations):
            observations = T(observations, device=self.device, dtype=torch.float)
        if not torch.is_tensor(action_mask) and action_mask is not None:
            action_mask = torch.tensor(action_mask, dtype=torch.float).to(self.device)

        # print(f"Observations: {observations.shape} {observations}")
        with torch.no_grad():
            continuous_logits, discrete_logits = self.actor(
                x=observations, action_mask=action_mask, gumbel=False, debug=False
            )
        # print(f"continuous_logits: {continuous_logits.shape} {continuous_logits}")
        # print(f"discrete_logits: {discrete_logits[0].shape} {discrete_logits}")
        # if len(continuous_logits.shape) == 1:
        # continuous_logits = continuous_logits.unsqueeze(0)
        try:
            action_logstd = self.actor_logstd.expand_as(continuous_logits)
            action_std = torch.exp(action_logstd)
            continuous_dist = torch.distributions.Normal(
                loc=continuous_logits,
                scale=action_std,
            )
        except Exception as e:
            print(
                f"bad stuff, {continuous_logits}, {discrete_logits}, {observations}, {action_mask} {e}"
            )
            with torch.no_grad():
                continuous_logits, discrete_logits = self.actor(
                    x=observations * 0, action_mask=None, gumbel=False, debug=False
                )
            print(
                f"still bad?, {continuous_logits}, {discrete_logits}, {observations*0}, {action_mask}"
            )
            action_logstd = self.actor_logstd.expand_as(continuous_logits)
            action_std = torch.exp(action_logstd)
            continuous_dist = torch.distributions.Normal(
                loc=continuous_logits,
                scale=action_std,
            )

        # print(discrete_logits)
        discrete_actions, discrete_log_probs = self._sample_multi_discrete(
            discrete_logits
        )
        # print(continuous_logits)

        continuous_actions = continuous_dist.sample()
        # print(continuous_actions)
        # exit()
        continuous_log_probs = continuous_dist.log_prob(continuous_actions)
        # print(continuous_log_probs)
        vals = self.critic(observations)
        return (
            discrete_actions.detach().cpu().numpy(),
            continuous_actions.detach().cpu().numpy(),
            discrete_log_probs.detach().cpu().numpy(),
            continuous_log_probs.detach().cpu().numpy(),
            vals.detach().cpu().numpy(),
        )

    # takes the observations and returns the action with the highest probability
    def ego_actions(self, observations, action_mask=None):
        with torch.no_grad():
            continuous_actions, discrete_action_activations = self.actor(
                observations, action_mask, gumbel=False
            )
            if len(continuous_actions.shape) == 1:
                continuous_actions = continuous_actions.unsqueeze(0)
            # Ignore the continuous actions std for ego action
            discrete_actions = torch.zeros(
                (observations.shape[0], len(discrete_action_activations)),
                device=self.device,
                dtype=torch.float32,
            )
            for i, activation in enumerate(discrete_action_activations):
                discrete_actions[:, i] = torch.argmax(activation, dim=1)
            return discrete_actions, continuous_actions

    def imitation_learn(self, observations, actions, action_mask=None):
        if not torch.is_tensor(actions):
            actions = torch.tensor(actions, dtype=torch.int).to(self.device)
        if not torch.is_tensor(observations):
            observations = torch.tensor(observations, dtype=torch.float).to(self.device)

        act, probs, log_probs = self.actor.evaluate(
            observations, action_mask=action_mask
        )
        # max_actions = act.argmax(dim=-1, keepdim=True)
        # loss is MSE loss beteen the actions and the predicted actions
        oh_actions = torch.nn.functional.one_hot(
            actions.squeeze(-1), self.actor_size
        ).float()
        # print(oh_actions.shape, probs.shape)
        loss = torch.nn.functional.cross_entropy(probs, oh_actions, reduction="mean")
        self.actor_optimizer.zero_grad()
        loss.backward()
        self.actor_optimizer.step()

        return loss.item()  # loss

    def utility_function(self, observations, actions=None):
        if not torch.is_tensor(observations):
            observations = torch.tensor(observations, dtype=torch.float).to(self.device)
        if actions is not None:
            return self.critic(observations, actions)
        else:
            return self.critic(observations)
        # If actions are none then V(s)

    def expected_V(self, obs, legal_action):
        return self.critic(obs)

    def marl_learn(self, batch, agent_num, mixer, critic_only=False, debug=False):
        return super().marl_learn(batch, agent_num, mixer, critic_only, debug)

    def zero_grads(self):
        return 0

    def _get_disc_log_probs_entropy(self, logits, actions):
        log_probs = torch.zeros_like(actions, dtype=torch.float)
        dist = Categorical(logits=logits)
        log_probs = dist.log_prob(actions)
        return log_probs, dist.entropy().mean()

    def _get_cont_log_probs_entropy(self, logits, actions):
        log_probs = torch.zeros_like(actions, dtype=torch.float)
        dist = torch.distributions.Normal(
            loc=logits, scale=torch.exp(self.actor_logstd.expand_as(logits))
        )
        log_probs = dist.log_prob(actions)
        return log_probs, dist.entropy().mean()

    def _get_probs_and_entropy(self, batch: FlexiBatch, agent_num):
        cp, dp = self.actor(
            batch.obs[agent_num], action_mask=batch.action_mask[agent_num]
        )
        if len(self.discrete_action_dims) > 0:
            old_disc_log_probs = []
            old_disc_entropy = []
            for head in range(len(self.discrete_action_dims)):
                odlp, ode = self._get_disc_log_probs_entropy(
                    logits=dp[head],
                    actions=batch.discrete_actions[agent_num][:, head],
                )
                old_disc_log_probs.append(odlp)
                old_disc_entropy.append(ode)
        else:
            old_disc_log_probs = 0
            old_disc_entropy = 0

        if self.continuous_action_dim > 0:
            old_cont_log_probs, old_cont_entropy = self._get_cont_log_probs_entropy(
                logits=cp, actions=batch.continuous_actions[agent_num]
            )
        else:
            old_cont_log_probs = 0
            old_cont_entropy = 0

        return (
            old_disc_log_probs,
            old_disc_entropy,
            old_cont_log_probs,
            old_cont_entropy,
        )

    def _G(self, batch, agent_num):
        G = torch.zeros_like(batch.global_rewards).to(self.device)
        G[-1] = batch.global_rewards[-1]
        if batch.terminated[-1] < 0.5:
            G[-1] += self.gamma * self.critic(batch.obs[agent_num][-1]).squeeze(-1)

        for i in range(len(batch.global_rewards) - 2, -1, -1):
            G[i] = batch.global_rewards[i] + self.gamma * G[i + 1] * (
                1 - batch.terminated[i]
            )
        G = G.unsqueeze(-1)
        return G

    def _gae(self, batch, agent_num):
        with torch.no_grad():
            values = self.critic(batch.obs[agent_num]).squeeze(-1)
            num_steps = batch.global_rewards.shape[0]
            advantages = torch.zeros_like(batch.global_rewards).to(self.device)
            lastgaelam = 0
            for t in reversed(range(num_steps - 1)):
                nextnonterminal = 1.0 - batch.terminated[t + 1]
                nextvalues = values[t + 1]
                delta = (
                    batch.global_rewards[t]
                    + self.gamma * nextvalues * nextnonterminal
                    - values[t]
                )
                # print(delta)
                advantages[t] = lastgaelam = (
                    delta + self.gamma * self.gae_lambda * nextnonterminal * lastgaelam
                )
        #
        G = advantages + values
        return G, advantages

    def _td(self, batch, agent_num):
        reward_arr = batch.global_rewards
        with torch.no_grad():
            old_values = self.critic(batch.obs[agent_num]).squeeze(-1)

        td = torch.zeros_like(reward_arr).to(self.device)

        for t in range(len(reward_arr) - 1):
            td[t] = (
                reward_arr[t]
                + self.gamma * old_values[t + 1] * (1 - batch.terminated[t])
                - old_values[t]
            )
        G = td + old_values
        return G, td

    def reinforcement_learn(
        self,
        batch: FlexiBatch,
        agent_num=0,
        critic_only=False,
        debug=False,
        conenv=False,
    ):
        # print(f"Doing PPO learn for agent {agent_num}")
        # Update the critic with Bellman Equation
        # Monte Carlo Estimate of returns

        # # G = G / 100
        with torch.no_grad():
            if self.advantage_type == "gv":
                G = self._G(batch, agent_num)
                advantages = G - self.critic(batch.obs[agent_num])
            elif self.advantage_type == "gae":
                G, advantages = self._gae(batch, agent_num)
            elif self.advantage_type == "a2c":
                G, advantages = self._td(batch, agent_num)
            elif self.advantage_type == "constant":
                G = self._G(batch, agent_num)
                advantages = G - G.mean()
            elif self.advantage_type == "g":
                G = self._G(batch, agent_num)
                advantages = G
            if self.norm_advantages:
                advantages = (advantages - advantages.mean()) / (
                    advantages.std() + 1e-8
                )
        avg_actor_loss = 0
        avg_critic_loss = 0
        # Update the actor
        action_mask = None
        if batch.action_mask is not None:
            action_mask = batch.action_mask[agent_num]

        bsize = len(batch.global_rewards)
        nbatch = bsize // self.mini_batch_size
        mini_batch_indices = np.arange(len(batch.global_rewards))
        np.random.shuffle(mini_batch_indices)

        for epoch in range(self.n_epochs):

            for bstart in range(0, bsize, self.mini_batch_size):
                # Get Critic Loss
                bend = bstart + self.mini_batch_size
                indices = mini_batch_indices[bstart:bend]

                V_current = self.critic(batch.obs[agent_num, indices])
                critic_loss = 0.5 * ((V_current - G[indices]) ** 2).mean()

                if not critic_only:
                    cont_probs, disc_probs = self.actor(
                        batch.obs[agent_num, indices],
                        action_mask=action_mask,  # TODO fix action mask by indices
                    )
                    actor_loss = 0

                    if self.continuous_action_dim > 0:
                        print(cont_probs.shape)
                        input("what is up with continuous probabilities")
                        cont_probs = cont_probs
                        continuous_dist = torch.distributions.Normal(
                            loc=cont_probs,
                            scale=torch.exp(self.actor_logstd.expand_as(cont_probs)),
                        )
                        continuous_log_probs = continuous_dist.log_prob(
                            batch.continuous_actions[agent_num]
                        )
                        continuous_policy_gradient = (
                            continuous_log_probs * advantages[mini_batch_indices]
                        )

                        actor_loss += (
                            -self.policy_loss * continuous_policy_gradient.mean()
                            + self.entropy_loss * continuous_dist.entropy().mean()
                        )

                    for head in range(len(self.discrete_action_dims)):
                        probs = disc_probs[head]  # Categorical()
                        dist = Categorical(probs=probs)
                        entropy = dist.entropy().mean()
                        selected_log_probs = dist.log_prob(
                            batch.discrete_actions[agent_num][mini_batch_indices, head]
                        )

                        discrete_policy_gradient = -selected_log_probs * advantages

                        actor_loss += (
                            self.policy_loss * discrete_policy_gradient.mean()
                            - self.entropy_loss * entropy
                        )

                    self.optimizer.zero_grad()
                    loss = actor_loss + critic_loss * self.critic_loss_coef
                    loss.backward()
                    try:
                        torch.nn.utils.clip_grad_norm_(
                            self.total_params, 0.5, error_if_nonfinite=True
                        )
                    except Exception as e:
                        print(f"Error in clipping {e}")
                        print(actor_loss.item(), critic_loss.item())
                        print(
                            f"surr1: {discrete_policy_gradient}, {continuous_policy_gradient}"
                        )
                        print(disc_probs)
                        paramvec = torch.nn.utils.parameters_to_vector(
                            self.total_params
                        )
                        print(max(paramvec), min(paramvec), torch.linalg.norm(paramvec))
                        print("paramvec", paramvec.isnan().sum())
                        print("loss", loss.isnan().sum())
                        print("entropy", entropy.isnan().sum())
                        # exit()
                    self.optimizer.step()

                    avg_actor_loss += actor_loss.item()
                    avg_critic_loss += critic_loss.item()
            avg_actor_loss /= nbatch
            avg_critic_loss /= nbatch
            # print(f"actor_loss: {actor_loss.item()}")

        avg_actor_loss /= self.n_epochs
        avg_critic_loss /= self.n_epochs
        # print(avg_actor_loss, critic_loss.item())
        return avg_actor_loss, avg_critic_loss

    def save(self, checkpoint_path):
        print("Save not implemeted")

    def load(self, checkpoint_path):
        print("Load not implemented")


if __name__ == "__main__":
    obs_dim = 3
    continuous_action_dim = 2
    agent = PG(
        obs_dim=obs_dim,
        continuous_action_dim=continuous_action_dim,
        discrete_action_dims=[4, 5],
        hidden_dims=[32, 32],
        device="gpu",
        lr=0.001,
        activation="relu",
        advantage_type="G",
        norm_advantages=True,
        mini_batch_size=7,
    )
    obs = np.random.rand(obs_dim).astype(np.float32)
    obs_ = np.random.rand(obs_dim).astype(np.float32)
    obs_batch = np.random.rand(14, obs_dim).astype(np.float32)
    obs_batch_ = obs_batch + 0.1

    dacs = np.stack(
        (np.random.randint(0, 4, size=(14)), np.random.randint(0, 5, size=(14))),
        axis=-1,
    )
    print(dacs.shape)
    print(dacs)
