from Agent import ValueS, MixedActor, Agent
import torch
from flexibuff import FlexiBatch
from torch.distributions import Categorical
from Util import T


class PPO(Agent):
    def __init__(
        self,
        obs_dim,
        continuous_action_dim=0,
        max_actions=None,
        min_actions=None,
        discrete_action_dims=None,
        lr=2.5e-4,
        gamma=0.99,
        eps_clip=0.2,
        n_epochs=4,
        device="cpu",
        entropy_loss=0.01,
        hidden_dims=[256, 256],
        activation="relu",
    ):
        super().__init__()
        assert (
            continuous_action_dim > 0 or discrete_action_dims is not None
        ), "At least one action dim should be provided"
        self.gae_lambda = 0.95
        self.device = device
        self.gamma = gamma
        self.eps_clip = eps_clip
        self.obs_dim = obs_dim
        self.continuous_action_dim = continuous_action_dim
        self.discrete_action_dims = discrete_action_dims
        self.n_epochs = n_epochs
        self.activation = activation

        self.policy_loss = 1
        self.critic_loss = 1
        self.entropy_loss = entropy_loss

        self.actor = MixedActor(
            obs_dim=obs_dim,
            continuous_action_dim=continuous_action_dim,
            discrete_action_dims=discrete_action_dims,
            max_actions=max_actions,
            min_actions=min_actions,
            hidden_dims=hidden_dims,
            device=device,
            orthogonal_init=False,
            activation="relu",
        )

        self.critic = ValueS(
            obs_dim=obs_dim,
            hidden_dim=256,
            device=self.device,
            orthogonal_init=False,
            activation="relu",
        )
        self.total_params = list(self.critic.parameters()) + list(
            self.actor.parameters()
        )
        self.optimizer = torch.optim.Adam(self.total_params, lr=lr)
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
            continuous_dist = torch.distributions.Normal(
                loc=continuous_logits[: self.continuous_action_dim],
                scale=torch.exp(continuous_logits[self.continuous_action_dim :]),
            )
        except:
            print(
                f"bad stuff, {continuous_logits}, {discrete_logits}, {observations}, {action_mask}"
            )
            with torch.no_grad():
                continuous_logits, discrete_logits = self.actor(
                    x=observations * 0, action_mask=None, gumbel=False, debug=False
                )
            print(
                f"still bad?, {continuous_logits}, {discrete_logits}, {observations*0}, {action_mask}"
            )
            continuous_dist = torch.distributions.Normal(
                loc=continuous_logits[:, : self.continuous_action_dim],
                scale=torch.exp(continuous_logits[:, self.continuous_action_dim :]),
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
            return discrete_actions, continuous_actions[:, : self.continuous_action_dim]

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

    def reinforcement_learn(
        self, batch: FlexiBatch, agent_num=0, critic_only=False, debug=False
    ):
        # print(f"Doing PPO learn for agent {agent_num}")
        # Update the critic with Bellman Equation
        # Monte Carlo Estimate of returns
        # G = torch.zeros_like(batch.global_rewards).to(self.device)
        # G[-1] = batch.global_rewards[-1]
        # for i in range(len(batch.global_rewards) - 2, -1, -1):
        #     G[i] = batch.global_rewards[i] + self.gamma * G[i + 1] * (
        #         1 - batch.terminated[i]
        #     )
        # G = G.unsqueeze(-1)
        # # G = G / 100
        # with torch.no_grad():
        #     advantages = G - self.critic(batch.obs[agent_num])
        # reward_arr = batch.global_rewards
        # with torch.no_grad():
        #    old_values = self.critic(batch.obs[agent_num]).squeeze(-1)

        # advantage = torch.zeros_like(reward_arr).to(self.device)

        # for t in range(len(reward_arr) - 1):
        #     discount = 1
        #     a_t = 0
        #     for k in range(t, len(reward_arr) - 1):
        #         a_t += discount * (
        #             reward_arr[k]
        #             + self.gamma * old_values[k + 1] * (1 - batch.terminated[k])
        #             - old_values[k]
        #         )
        #         discount *= self.gamma * self.gae_lambda
        #     advantage[t] = a_t

        # G = advantage + old_values
        ########################################################################################
        # print(G)
        # print(batch.terminated)
        # advantage = T.tensor(advantage).to(self.actor.device)
        # values = T.tensor(values).to(self.actor.device)

        with torch.no_grad():
            values = self.critic(batch.obs[agent_num]).squeeze(-1)
            # print(values)
            # if batch.terminated[-1]:
            #    values[-1] = batch.global_rewards[-1]
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

        avg_actor_loss = 0
        # Update the actor
        action_mask = None
        if batch.action_mask is not None:
            action_mask = batch.action_mask[agent_num]

        with torch.no_grad():
            cp, dp = self.actor(batch.obs[agent_num], action_mask=action_mask)
            old_probs = dp[0]  # Categorical()
            old_log_probs = torch.log(
                old_probs.gather(
                    -1, batch.discrete_actions[agent_num][:, 0].unsqueeze(-1)
                )
            )
        for epoch in range(self.n_epochs):

            V_current = self.critic(batch.obs[agent_num])
            critic_loss = 0.5 * ((V_current - G) ** 2).mean()

            if not critic_only:
                # with torch.no_grad():
                #     advantages = G - self.critic(batch.obs[agent_num])
                #     advantages = (advantages - advantages.mean()) / (
                #         advantages.std() + 1e-8
                #     )
                # print("doing actor")

                cont_probs, disc_probs = self.actor(
                    batch.obs[agent_num], action_mask=action_mask
                )
                actor_loss = 0

                if self.continuous_action_dim > 0:
                    cont_probs = cont_probs.squeeze(0)
                    # continuous_dist = torch.distributions.Normal(
                    #     loc=cont_probs[:, : self.continuous_action_dim],
                    #     scale=torch.exp(cont_probs[:, self.continuous_action_dim :]),
                    # )
                    # continuous_log_probs = continuous_dist.log_prob(
                    #     batch.continuous_actions[
                    #         agent_num, :, : self.continuous_action_dim
                    #     ]
                    # )
                    # # print(batch.continuous_log_probs.shape)
                    # # print(continuous_log_probs.shape)
                    # # print(batch.continuous_actions.shape)
                    # # exit()
                    # continuous_ratios = torch.exp(
                    #     continuous_log_probs - batch.continuous_log_probs[agent_num]
                    # )
                    # continuous_surr1 = continuous_ratios * advantages
                    # continuous_surr2 = (
                    #     torch.clamp(
                    #         continuous_ratios, 1 - self.eps_clip, 1 + self.eps_clip
                    #     )
                    #     * advantages
                    # )
                    # actor_loss += (
                    #     -self.policy_loss
                    #     * torch.min(continuous_surr1, continuous_surr2).mean()
                    #     + self.entropy_loss * continuous_dist.entropy().mean()
                    # )

                for head in range(len(self.discrete_action_dims)):
                    probs = disc_probs[head]  # Categorical()
                    selected_log_probs = torch.log(
                        probs.gather(
                            -1, batch.discrete_actions[agent_num][:, head].unsqueeze(-1)
                        )
                    )
                    entropy = Categorical(probs=probs).entropy().mean()
                    # print(entropy)
                    # dist_entropy = dist.entropy()
                    # selected_log_probs = dist.log_prob(
                    #    batch.discrete_actions[agent_num][head]
                    # )
                    # print(
                    #     (selected_log_probs - old_log_probs).max(),
                    #     (selected_log_probs - old_log_probs).min(),
                    # )
                    # print(selected_log_probs)
                    # print(old_log_probs)
                    ratios = torch.exp(
                        (selected_log_probs.clip(-10, 10) - old_log_probs.clip(-10, 10))
                    ).squeeze(-1)
                    # print(ratios)
                    # Calculate surrogate loss
                    surr1 = ratios * advantages
                    # actor_loss = -(ratios * advantage).mean()
                    surr2 = (
                        torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip)
                        * advantages
                    )
                    actor_loss += (
                        self.policy_loss * (-torch.min(surr1, surr2).mean())
                        - self.entropy_loss * entropy
                    )

                self.optimizer.zero_grad()
                loss = actor_loss + critic_loss
                loss.backward()
                try:
                    torch.nn.utils.clip_grad_norm_(
                        self.total_params, 0.5, error_if_nonfinite=True
                    )
                except:
                    input("print rest?")
                    print("Error in clipping")
                    print(actor_loss.item(), critic_loss.item())
                    paramvec = torch.nn.utils.parameters_to_vector(self.total_params)
                    # paramvec has nan

                    print(max(paramvec), min(paramvec), torch.linalg.norm(paramvec))
                    print(f"surr1: {surr1}, surr2: {surr2}")
                    print(selected_log_probs, old_log_probs)
                    print(disc_probs)
                    print("paramvec", paramvec.isnan().sum())
                    print("surr1", surr1.isnan().sum())
                    print("surr2", surr2.isnan().sum())
                    print("loss", loss.isnan().sum())
                    print("entropy", entropy.isnan().sum())
                    # exit()
                self.optimizer.step()

                avg_actor_loss += actor_loss.item()
            # print(f"actor_loss: {actor_loss.item()}")

        avg_actor_loss /= self.n_epochs
        # print(avg_actor_loss, critic_loss.item())
        return avg_actor_loss, critic_loss.item()

    def save(self, checkpoint_path):
        print("Save not implemeted")

    def load(self, checkpoint_path):
        print("Load not implemented")
