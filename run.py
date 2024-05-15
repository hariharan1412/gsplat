# ruff: noqa: E741
# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
NeRF implementation that combines many recent advancements.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type, Union

import imageio.v2 as imageio
import numpy as np
import torch
import tqdm
import json

# from nerfdata.dataset.colmap.dataset import Dataset
from pytorch_msssim import SSIM
from torch.nn import Parameter
from torchmetrics.image import PeakSignalNoiseRatio
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
from typing_extensions import Literal
import matplotlib.pyplot as plt

from gsplat._torch_impl import quat_to_rotmat
from gsplat.experimental.cuda import (
    isect_offset_encode,
    isect_tiles,
    projection,
    quat_scale_to_covar_perci,
    rasterize_to_pixels,
)
from gsplat.project_gaussians import project_gaussians
from gsplat.rasterize import rasterize_gaussians
from gsplat.sh import num_sh_bases, spherical_harmonics

# Benchmark on Tesla V100-SXM2-16GB
# * UseGsplatV2 = False:
# Training 03m08s
# Eval metrics on 24 images {'psnr': 26.135067860285442, 'ssim': 0.8051311001181602, 'lpips': 0.150730239537855}
# Eval time: 0.2185s
# * UseGsplatV2 = True:
# Training 03m14s
# Eval metrics on 24 images {'psnr': 26.129370371500652, 'ssim': 0.8068777024745941, 'lpips': 0.1478917102018992}
# Eval time: 0.1619s
UseGsplatV2 = True


def random_quat_tensor(N):
    """
    Defines a random quaternion tensor of shape (N, 4)
    """
    u = torch.rand(N)
    v = torch.rand(N)
    w = torch.rand(N)
    return torch.stack(
        [
            torch.sqrt(1 - u) * torch.sin(2 * math.pi * v),
            torch.sqrt(1 - u) * torch.cos(2 * math.pi * v),
            torch.sqrt(u) * torch.sin(2 * math.pi * w),
            torch.sqrt(u) * torch.cos(2 * math.pi * w),
        ],
        dim=-1,
    )


def RGB2SH(rgb):
    """
    Converts from RGB values [0,1] to the 0th spherical harmonic coefficient
    """
    C0 = 0.28209479177387814
    return (rgb - 0.5) / C0


def SH2RGB(sh):
    """
    Converts from the 0th spherical harmonic coefficient to RGB values [0,1]
    """
    C0 = 0.28209479177387814
    return sh * C0 + 0.5


@dataclass
class SplatfactoModelConfig:
    """Splatfacto Model Config, nerfstudio's implementation of Gaussian Splatting"""

    warmup_length: int = 500
    """period of steps where refinement is turned off"""
    refine_every: int = 100
    """period of steps where gaussians are culled and densified"""
    resolution_schedule: int = 3000
    """training starts at 1/d resolution, every n steps this is doubled"""
    background_color: Literal["random", "black", "white"] = "random"
    """Whether to randomize the background color."""
    num_downscales: int = 2
    """at the beginning, resolution is 1/2^d, where d is this number"""
    cull_alpha_thresh: float = 0.1
    """threshold of opacity for culling gaussians. One can set it to a lower value (e.g. 0.005) for higher quality."""
    cull_scale_thresh: float = 0.5
    """threshold of scale for culling huge gaussians"""
    continue_cull_post_densification: bool = True
    """If True, continue to cull gaussians post refinement"""
    reset_alpha_every: int = 30
    """Every this many refinement steps, reset the alpha"""
    densify_grad_thresh: float = 0.0002
    """threshold of positional gradient norm for densifying gaussians"""
    densify_size_thresh: float = 0.01
    """below this size, gaussians are *duplicated*, otherwise split"""
    n_split_samples: int = 2
    """number of samples to split gaussians into"""
    sh_degree_interval: int = 1000
    """every n intervals turn on another sh degree"""
    cull_screen_size: float = 0.15
    """if a gaussian is more than this percent of screen space, cull it"""
    split_screen_size: float = 0.05
    """if a gaussian is more than this percent of screen space, split it"""
    stop_screen_size_at: int = 4000
    """stop culling/splitting at this step WRT screen size of gaussians"""
    random_init: bool = False
    """whether to initialize the positions uniformly randomly (not SFM points)"""
    num_random: int = 10000
    """Number of gaussians to initialize if random init is used"""
    # random_scale: float = 10.0
    # "Size of the cube to initialize random gaussians within"
    ssim_lambda: float = 0.2
    """weight of ssim loss"""
    stop_split_at: int = 15000
    """stop splitting at this step"""
    sh_degree: int = 3
    """maximum degree of spherical harmonics to use"""
    use_scale_regularization: bool = False
    """If enabled, a scale regularization introduced in PhysGauss (https://xpandora.github.io/PhysGaussian/) is used for reducing huge spikey gaussians."""
    max_gauss_ratio: float = 10.0
    """threshold of ratio of gaussian max to min scale before applying regularization
    loss from the PhysGaussian paper
    """
    output_depth_during_training: bool = False
    """If True, output depth during training. Otherwise, only output depth during evaluation."""
    rasterize_mode: Literal["classic", "antialiased"] = "classic"
    """
    Classic mode of rendering will use the EWA volume splatting with a [0.3, 0.3] screen space blurring kernel. This
    approach is however not suitable to render tiny gaussians at higher or lower resolution than the captured, which
    results "aliasing-like" artifacts. The antialiased mode overcomes this limitation by calculating compensation factors
    and apply them to the opacities of gaussians to preserve the total integrated density of splats.

    However, PLY exported with antialiased rasterize mode is not compatible with classic mode. Thus many web viewers that
    were implemented for classic mode can not render antialiased mode PLY properly without modifications.
    """


class SplatfactoModel(torch.nn.Module):
    """Nerfstudio's implementation of Gaussian Splatting

    Args:
        config: Splatfacto configuration to instantiate model
    """

    def __init__(
        self,
        num_train_data: int,
        config: SplatfactoModelConfig = SplatfactoModelConfig(),
        seed_points: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        aabb: Optional[Tensor] = None,
    ):
        super().__init__()
        self.num_train_data = num_train_data
        self.config = config
        self.seed_points = seed_points
        self.aabb = aabb
        self.populate_modules()

    def populate_modules(self):
        if self.seed_points is not None and not self.config.random_init:
            means = torch.nn.Parameter(self.seed_points[0])  # (Location, Color)
        else:
            assert self.aabb is not None
            minimums = self.aabb[:3]
            maximums = self.aabb[3:]
            center = (minimums + maximums) / 2
            extent = (maximums - minimums) / 2
            means = torch.nn.Parameter(
                (torch.rand((self.config.num_random, 3)) - 0.5) * extent + center
            )
        self.xys_grad_norm = None
        self.max_2Dsize = None
        distances, _ = self.k_nearest_sklearn(means.data, 3)
        distances = torch.from_numpy(distances)
        # find the average of the three nearest neighbors for each point and use that as the scale
        avg_dist = distances.mean(dim=-1, keepdim=True)
        scales = torch.nn.Parameter(torch.log(avg_dist.repeat(1, 3)))
        num_points = means.shape[0]
        quats = torch.nn.Parameter(random_quat_tensor(num_points))
        dim_sh = num_sh_bases(self.config.sh_degree)

        if (
            self.seed_points is not None
            and not self.config.random_init
            # We can have colors without points.
            and self.seed_points[1].shape[0] > 0
        ):
            shs = torch.zeros((self.seed_points[1].shape[0], dim_sh, 3)).float().cuda()
            if self.config.sh_degree > 0:
                shs[:, 0, :3] = RGB2SH(self.seed_points[1] / 255)
                shs[:, 1:, 3:] = 0.0
            else:
                print("use color only optimization with sigmoid activation")
                shs[:, 0, :3] = torch.logit(self.seed_points[1] / 255, eps=1e-10)
            features_dc = torch.nn.Parameter(shs[:, 0, :])
            features_rest = torch.nn.Parameter(shs[:, 1:, :])
        else:
            features_dc = torch.nn.Parameter(torch.rand(num_points, 3))
            features_rest = torch.nn.Parameter(torch.zeros((num_points, dim_sh - 1, 3)))

        opacities = torch.nn.Parameter(torch.logit(0.1 * torch.ones(num_points, 1)))
        self.gauss_params = torch.nn.ParameterDict(
            {
                "means": means,
                "scales": scales,
                "quats": quats,
                "features_dc": features_dc,
                "features_rest": features_rest,
                "opacities": opacities,
            }
        )
        self.psnr = PeakSignalNoiseRatio(data_range=1.0)
        self.ssim = SSIM(data_range=1.0, size_average=True, channel=3)
        self.lpips = LearnedPerceptualImagePatchSimilarity(normalize=True)
        self.step = 0

        if self.config.background_color == "random":
            self.background_color = torch.tensor(
                [0.1490, 0.1647, 0.2157]
            )  # This color is the same as the default background color in Viser. This would only affect the background color when rendering.
        elif self.config.background_color == "white":
            self.background_color = torch.ones(3)
        elif self.config.background_color == "black":
            self.background_color = torch.zeros(3)
        else:
            raise ValueError(
                f"Unknown background color: {self.config.background_color}"
            )

    @property
    def colors(self):
        if self.config.sh_degree > 0:
            return SH2RGB(self.features_dc)
        else:
            return torch.sigmoid(self.features_dc)

    @property
    def shs_0(self):
        return self.features_dc

    @property
    def shs_rest(self):
        return self.features_rest

    @property
    def num_points(self):
        return self.means.shape[0]

    @property
    def means(self):
        return self.gauss_params["means"]

    @property
    def scales(self):
        return self.gauss_params["scales"]

    @property
    def quats(self):
        return self.gauss_params["quats"]

    @property
    def features_dc(self):
        return self.gauss_params["features_dc"]

    @property
    def features_rest(self):
        return self.gauss_params["features_rest"]

    @property
    def opacities(self):
        return self.gauss_params["opacities"]

    def load_state_dict(self, dict, **kwargs):  # type: ignore
        # resize the parameters to match the new number of points
        self.step = 30000
        if "means" in dict:
            # For backwards compatibility, we remap the names of parameters from
            # means->gauss_params.means since old checkpoints have that format
            for p in [
                "means",
                "scales",
                "quats",
                "features_dc",
                "features_rest",
                "opacities",
            ]:
                dict[f"gauss_params.{p}"] = dict[p]
        newp = dict["gauss_params.means"].shape[0]
        for name, param in self.gauss_params.items():
            old_shape = param.shape
            new_shape = (newp,) + old_shape[1:]
            self.gauss_params[name] = torch.nn.Parameter(torch.zeros(new_shape))
        super().load_state_dict(dict, **kwargs)

    def k_nearest_sklearn(self, x: torch.Tensor, k: int):
        """
            Find k-nearest neighbors using sklearn's NearestNeighbors.
        x: The data tensor of shape [num_samples, num_features]
        k: The number of neighbors to retrieve
        """
        # Convert tensor to numpy array
        x_np = x.cpu().numpy()

        # Build the nearest neighbors model
        from sklearn.neighbors import NearestNeighbors

        nn_model = NearestNeighbors(
            n_neighbors=k + 1, algorithm="auto", metric="euclidean"
        ).fit(x_np)

        # Find the k-nearest neighbors
        distances, indices = nn_model.kneighbors(x_np)

        # Exclude the point itself from the result and return
        return distances[:, 1:].astype(np.float32), indices[:, 1:].astype(np.float32)

    def remove_from_optim(self, optimizer, deleted_mask, new_params):
        """removes the deleted_mask from the optimizer provided"""
        assert len(new_params) == 1
        # assert isinstance(optimizer, torch.optim.Adam), "Only works with Adam"

        param = optimizer.param_groups[0]["params"][0]
        param_state = optimizer.state[param]
        del optimizer.state[param]

        # Modify the state directly without deleting and reassigning.
        if "exp_avg" in param_state:
            param_state["exp_avg"] = param_state["exp_avg"][~deleted_mask]
            param_state["exp_avg_sq"] = param_state["exp_avg_sq"][~deleted_mask]

        # Update the parameter in the optimizer's param group.
        del optimizer.param_groups[0]["params"][0]
        del optimizer.param_groups[0]["params"]
        optimizer.param_groups[0]["params"] = new_params
        optimizer.state[new_params[0]] = param_state

    def remove_from_all_optim(self, optimizers: Dict, deleted_mask):
        param_groups = self.get_gaussian_param_groups()
        for group, param in param_groups.items():
            self.remove_from_optim(optimizers[group], deleted_mask, param)
        torch.cuda.empty_cache()

    def dup_in_optim(self, optimizer, dup_mask, new_params, n=2):
        """adds the parameters to the optimizer"""
        param = optimizer.param_groups[0]["params"][0]
        param_state = optimizer.state[param]
        if "exp_avg" in param_state:
            repeat_dims = (n,) + tuple(
                1 for _ in range(param_state["exp_avg"].dim() - 1)
            )
            param_state["exp_avg"] = torch.cat(
                [
                    param_state["exp_avg"],
                    torch.zeros_like(param_state["exp_avg"][dup_mask.squeeze()]).repeat(
                        *repeat_dims
                    ),
                ],
                dim=0,
            )
            param_state["exp_avg_sq"] = torch.cat(
                [
                    param_state["exp_avg_sq"],
                    torch.zeros_like(
                        param_state["exp_avg_sq"][dup_mask.squeeze()]
                    ).repeat(*repeat_dims),
                ],
                dim=0,
            )
        del optimizer.state[param]
        optimizer.state[new_params[0]] = param_state
        optimizer.param_groups[0]["params"] = new_params
        del param

    def dup_in_all_optim(self, optimizers, dup_mask, n):
        param_groups = self.get_gaussian_param_groups()
        for group, param in param_groups.items():
            self.dup_in_optim(optimizers[group], dup_mask, param, n)

    def after_train(self, step: int):
        assert step == self.step
        # to save some training time, we no longer need to update those stats post refinement
        if self.step >= self.config.stop_split_at:
            return
        with torch.no_grad():
            # keep track of a moving average of grad norms
            visible_mask = (self.radii > 0).flatten()
            assert self.xys.grad is not None
            grads = self.xys.grad.detach().norm(dim=-1)
            # print(f"grad norm min {grads.min().item()} max {grads.max().item()} mean {grads.mean().item()} size {grads.shape}")
            if self.xys_grad_norm is None:
                self.xys_grad_norm = grads
                self.vis_counts = torch.ones_like(self.xys_grad_norm)
            else:
                assert self.vis_counts is not None
                self.vis_counts[visible_mask] = self.vis_counts[visible_mask] + 1
                self.xys_grad_norm[visible_mask] = (
                    grads[visible_mask] + self.xys_grad_norm[visible_mask]
                )

            # update the max screen size, as a ratio of number of pixels
            if self.max_2Dsize is None:
                self.max_2Dsize = torch.zeros_like(self.radii, dtype=torch.float32)
            newradii = self.radii.detach()[visible_mask]
            self.max_2Dsize[visible_mask] = torch.maximum(
                self.max_2Dsize[visible_mask],
                newradii / float(max(self.last_size[0], self.last_size[1])),
            )

    def set_background(self, background_color: torch.Tensor):
        assert background_color.shape == (3,)
        self.background_color = background_color

    def refinement_after(self, optimizers: Dict, step):
        assert step == self.step
        if self.step <= self.config.warmup_length:
            return
        with torch.no_grad():
            # Offset all the opacity reset logic by refine_every so that we don't
            # save checkpoints right when the opacity is reset (saves every 2k)
            # then cull
            # only split/cull if we've seen every image since opacity reset
            reset_interval = self.config.reset_alpha_every * self.config.refine_every
            do_densification = (
                self.step < self.config.stop_split_at
                and self.step % reset_interval
                > self.num_train_data + self.config.refine_every
            )
            if do_densification:
                # then we densify
                assert (
                    self.xys_grad_norm is not None
                    and self.vis_counts is not None
                    and self.max_2Dsize is not None
                )
                avg_grad_norm = (
                    (self.xys_grad_norm / self.vis_counts)
                    * 0.5
                    * max(self.last_size[0], self.last_size[1])
                )
                high_grads = (avg_grad_norm > self.config.densify_grad_thresh).squeeze()
                splits = (
                    self.scales.exp().max(dim=-1).values
                    > self.config.densify_size_thresh
                ).squeeze()
                if self.step < self.config.stop_screen_size_at:
                    splits |= (
                        self.max_2Dsize > self.config.split_screen_size
                    ).squeeze()
                splits &= high_grads
                nsamps = self.config.n_split_samples
                split_params = self.split_gaussians(splits, nsamps)

                dups = (
                    self.scales.exp().max(dim=-1).values
                    <= self.config.densify_size_thresh
                ).squeeze()
                dups &= high_grads
                dup_params = self.dup_gaussians(dups)
                for name, param in self.gauss_params.items():
                    self.gauss_params[name] = torch.nn.Parameter(
                        torch.cat(
                            [param.detach(), split_params[name], dup_params[name]],
                            dim=0,
                        )
                    )

                # append zeros to the max_2Dsize tensor
                self.max_2Dsize = torch.cat(
                    [
                        self.max_2Dsize,
                        torch.zeros_like(split_params["scales"][:, 0]),
                        torch.zeros_like(dup_params["scales"][:, 0]),
                    ],
                    dim=0,
                )

                split_idcs = torch.where(splits)[0]
                self.dup_in_all_optim(optimizers, split_idcs, nsamps)

                dup_idcs = torch.where(dups)[0]
                self.dup_in_all_optim(optimizers, dup_idcs, 1)

                # After a guassian is split into two new gaussians, the original one should also be pruned.
                splits_mask = torch.cat(
                    (
                        splits,
                        torch.zeros(
                            nsamps * splits.sum() + dups.sum(),
                            device=splits.device,
                            dtype=torch.bool,
                        ),
                    )
                )

                deleted_mask = self.cull_gaussians(splits_mask)
            elif (
                self.step >= self.config.stop_split_at
                and self.config.continue_cull_post_densification
            ):
                deleted_mask = self.cull_gaussians()
            else:
                # if we donot allow culling post refinement, no more gaussians will be pruned.
                deleted_mask = None

            if deleted_mask is not None:
                self.remove_from_all_optim(optimizers, deleted_mask)

            if (
                self.step < self.config.stop_split_at
                and self.step % reset_interval == self.config.refine_every
            ):
                # Reset value is set to be twice of the cull_alpha_thresh
                reset_value = self.config.cull_alpha_thresh * 2.0
                self.opacities.data = torch.clamp(
                    self.opacities.data,
                    max=torch.logit(torch.tensor(reset_value)).item(),
                )
                # reset the exp of optimizer
                optim = optimizers["opacities"]
                param = optim.param_groups[0]["params"][0]
                param_state = optim.state[param]
                param_state["exp_avg"] = torch.zeros_like(param_state["exp_avg"])
                param_state["exp_avg_sq"] = torch.zeros_like(param_state["exp_avg_sq"])

            self.xys_grad_norm = None
            self.vis_counts = None
            self.max_2Dsize = None

    def cull_gaussians(self, extra_cull_mask: Optional[torch.Tensor] = None):
        """
        This function deletes gaussians with under a certain opacity threshold
        extra_cull_mask: a mask indicates extra gaussians to cull besides existing culling criterion
        """
        n_bef = self.num_points
        # cull transparent ones
        culls = (
            torch.sigmoid(self.opacities) < self.config.cull_alpha_thresh
        ).squeeze()
        below_alpha_count = torch.sum(culls).item()
        toobigs_count = 0
        if extra_cull_mask is not None:
            culls = culls | extra_cull_mask
        if self.step > self.config.refine_every * self.config.reset_alpha_every:
            # cull huge ones
            toobigs = (
                torch.exp(self.scales).max(dim=-1).values
                > self.config.cull_scale_thresh
            ).squeeze()
            if self.step < self.config.stop_screen_size_at:
                # cull big screen space
                assert self.max_2Dsize is not None
                toobigs = (
                    toobigs | (self.max_2Dsize > self.config.cull_screen_size).squeeze()
                )
            culls = culls | toobigs
            toobigs_count = torch.sum(toobigs).item()
        for name, param in self.gauss_params.items():
            self.gauss_params[name] = torch.nn.Parameter(param[~culls])

        print(
            f"Culled {n_bef - self.num_points} gaussians "
            f"({below_alpha_count} below alpha thresh, {toobigs_count} too bigs, {self.num_points} remaining)"
        )

        return culls

    def split_gaussians(self, split_mask, samps):
        """
        This function splits gaussians that are too large
        """
        n_splits = split_mask.sum().item()
        print(
            f"Splitting {split_mask.sum().item()/self.num_points} gaussians: {n_splits}/{self.num_points}"
        )
        centered_samples = torch.randn(
            (samps * n_splits, 3), device=split_mask.device
        )  # Nx3 of axis-aligned scales
        scaled_samples = (
            torch.exp(self.scales[split_mask].repeat(samps, 1)) * centered_samples
        )  # how these scales are rotated
        quats = self.quats[split_mask] / self.quats[split_mask].norm(
            dim=-1, keepdim=True
        )  # normalize them first
        rots = quat_to_rotmat(quats.repeat(samps, 1))  # how these scales are rotated
        rotated_samples = torch.bmm(rots, scaled_samples[..., None]).squeeze()
        new_means = rotated_samples + self.means[split_mask].repeat(samps, 1)
        # step 2, sample new colors
        new_features_dc = self.features_dc[split_mask].repeat(samps, 1)
        new_features_rest = self.features_rest[split_mask].repeat(samps, 1, 1)
        # step 3, sample new opacities
        new_opacities = self.opacities[split_mask].repeat(samps, 1)
        # step 4, sample new scales
        size_fac = 1.6
        new_scales = torch.log(torch.exp(self.scales[split_mask]) / size_fac).repeat(
            samps, 1
        )
        self.scales[split_mask] = torch.log(
            torch.exp(self.scales[split_mask]) / size_fac
        )
        # step 5, sample new quats
        new_quats = self.quats[split_mask].repeat(samps, 1)
        out = {
            "means": new_means,
            "features_dc": new_features_dc,
            "features_rest": new_features_rest,
            "opacities": new_opacities,
            "scales": new_scales,
            "quats": new_quats,
        }
        for name, param in self.gauss_params.items():
            if name not in out:
                out[name] = param[split_mask].repeat(samps, 1)
        return out

    def dup_gaussians(self, dup_mask):
        """
        This function duplicates gaussians that are too small
        """
        n_dups = dup_mask.sum().item()
        print(
            f"Duplicating {dup_mask.sum().item()/self.num_points} gaussians: {n_dups}/{self.num_points}"
        )
        new_dups = {}
        for name, param in self.gauss_params.items():
            new_dups[name] = param[dup_mask]
        return new_dups

    def train_callback_before_iteration(self, step: int):
        self.step_cb(step)

    def train_callback_after_iteration(self, optimizers: Dict, step: int):
        self.after_train(step)
        if step % self.config.refine_every == 0:
            self.refinement_after(optimizers, step)

    def step_cb(self, step):
        self.step = step

    def get_gaussian_param_groups(self) -> Dict[str, List[Parameter]]:
        # Here we explicitly use the means, scales as parameters so that the user can override this function and
        # specify more if they want to add more optimizable params to gaussians.
        return {
            name: [self.gauss_params[name]]
            for name in [
                "means",
                "scales",
                "quats",
                "features_dc",
                "features_rest",
                "opacities",
            ]
        }

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Obtain the parameter groups for the optimizers

        Returns:
            Mapping of different parameter groups
        """
        gps = self.get_gaussian_param_groups()
        return gps

    def _get_downscale_factor(self):
        if self.training:
            return 2 ** max(
                (
                    self.config.num_downscales
                    - self.step // self.config.resolution_schedule
                ),
                0,
            )
        else:
            return 1

    def _downscale_if_required(self, image):
        d = self._get_downscale_factor()
        if d > 1:
            newsize = [image.shape[0] // d, image.shape[1] // d]

            # torchvision can be slow to import, so we do it lazily.
            import torchvision.transforms.functional as TF

            return TF.resize(image.permute(2, 0, 1), newsize, antialias=None).permute(
                1, 2, 0
            )
        return image

    def get_outputs(
        self,
        camera_to_world: torch.Tensor,
        K: torch.Tensor,
        width: int,
        height: int,
    ) -> Dict[str, Union[torch.Tensor, List]]:

        
        # print(" HELLO WORLD ")
        # raise Exception(" ITS WORKING ")
        """Takes in a Ray Bundle and returns a dictionary of outputs.

        Args:
            ray_bundle: Input bundle of rays. This raybundle should have all the
            needed information to compute the outputs.

        Returns:
            Outputs of model. (ie. rendered colors)
        """
        assert camera_to_world.shape == (4, 4) and K.shape == (3, 3)
        fx, fy = K[0, 0].item(), K[1, 1].item()
        cx, cy = K[0, 2].item(), K[1, 2].item()
        device = camera_to_world.device

        # get the background color
        if self.training:
            if self.config.background_color == "random":
                background = torch.rand(3, device=device)
            elif self.config.background_color == "white":
                background = torch.ones(3, device=device)
            elif self.config.background_color == "black":
                background = torch.zeros(3, device=device)
            else:
                background = self.background_color.to(device)
        else:
            background = self.background_color.to(device)

        crop_ids = None
        camera_downscale = self._get_downscale_factor()
        fx, fy = fx / camera_downscale, fy / camera_downscale
        cx, cy = cx / camera_downscale, cy / camera_downscale
        width, height = width // camera_downscale, height // camera_downscale

        # shift the camera to center of scene looking at center
        R = camera_to_world[:3, :3]  # 3 x 3
        T = camera_to_world[:3, 3:4]  # 3 x 1
        # # flip the z and y axes to align with gsplat conventions
        # R_edit = torch.diag(torch.tensor([1, -1, -1], device=device, dtype=R.dtype))
        # R = R @ R_edit
        # analytic matrix inverse to get world2camera matrix
        R_inv = R.T
        T_inv = -R_inv @ T
        viewmat = torch.eye(4, device=R.device, dtype=R.dtype)
        viewmat[:3, :3] = R_inv
        viewmat[:3, 3:4] = T_inv
        # calculate the FOV of the camera given fx and fy, width and height
        W, H = width, height
        self.last_size = (H, W)

        if crop_ids is not None:
            opacities_crop = self.opacities[crop_ids]
            means_crop = self.means[crop_ids]
            features_dc_crop = self.features_dc[crop_ids]
            features_rest_crop = self.features_rest[crop_ids]
            scales_crop = self.scales[crop_ids]
            quats_crop = self.quats[crop_ids]
        else:
            opacities_crop = self.opacities
            means_crop = self.means
            features_dc_crop = self.features_dc
            features_rest_crop = self.features_rest
            scales_crop = self.scales
            quats_crop = self.quats

        colors_crop = torch.cat(
            (features_dc_crop[:, None, :], features_rest_crop), dim=1
        )

        if UseGsplatV2:
            
            K = torch.tensor([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], device=device)
            covars, _ = quat_scale_to_covar_perci(
                quats_crop / quats_crop.norm(dim=-1, keepdim=True),
                torch.exp(scales_crop),
                compute_perci=False,
                triu=True,
            )
            radii, means2d, depths, conics = projection(
                means_crop, covars, viewmat[None, :], K[None, :], W, H
            )
            self.radii = radii.squeeze(0)
            self.xys = means2d.squeeze(0)
            comp = None

            # print(radii, means2d, depths, conics)
        else:
            BLOCK_WIDTH = (
                16  # this controls the tile size of rasterization, 16 is a good default
            )
            self.xys, depths, self.radii, conics, comp, num_tiles_hit, cov3d = project_gaussians(  # type: ignore
                means_crop,
                torch.exp(scales_crop),
                1,
                quats_crop / quats_crop.norm(dim=-1, keepdim=True),
                viewmat.squeeze()[:3, :],
                fx,
                fy,
                cx,
                cy,
                H,
                W,
                BLOCK_WIDTH,
            )  # type: ignore

            print(self.xys, depths, self.radii, conics, comp, num_tiles_hit, cov3d)

        if (self.radii).sum() == 0:
            rgb = background.repeat(H, W, 1)
            depth = background.new_ones(*rgb.shape[:2], 1) * 10
            accumulation = background.new_zeros(*rgb.shape[:2], 1)

            return {
                "rgb": rgb,
                "depth": depth,
                "accumulation": accumulation,
                "background": background,
            }

        # Important to allow xys grads to populate properly
        if self.training:
            self.xys.retain_grad()

        if self.config.sh_degree > 0:
            viewdirs = means_crop.detach() - camera_to_world.detach()[:3, 3]  # (N, 3)
            viewdirs = viewdirs / viewdirs.norm(dim=-1, keepdim=True)
            n = min(self.step // self.config.sh_degree_interval, self.config.sh_degree)
            rgbs = spherical_harmonics(n, viewdirs, colors_crop)
            rgbs = torch.clamp(rgbs + 0.5, min=0.0)  # type: ignore
        else:
            rgbs = torch.sigmoid(colors_crop[:, 0, :])

        if UseGsplatV2:
            tile_size = 16
            tile_width = math.ceil(W / tile_size)
            tile_height = math.ceil(H / tile_size)
            tiles_per_gauss, isect_ids, gauss_ids = isect_tiles(
                means2d, radii, depths, tile_size, tile_width, tile_height
            )
            isect_offsets = isect_offset_encode(isect_ids, 1, tile_width, tile_height)
            assert (tiles_per_gauss > 0).any()  # type: ignore
        else:
            assert (num_tiles_hit > 0).any()  # type: ignore

        # apply the compensation of screen space blurring to gaussians
        opacities = None
        if self.config.rasterize_mode == "antialiased":
            opacities = torch.sigmoid(opacities_crop) * comp[:, None]
        elif self.config.rasterize_mode == "classic":
            opacities = torch.sigmoid(opacities_crop)
        else:
            raise ValueError("Unknown rasterize_mode: %s", self.config.rasterize_mode)

        if UseGsplatV2:
            if self.config.output_depth_during_training or not self.training:
                feats = torch.cat((rgbs.unsqueeze(0), depths[..., None]), dim=-1)
            else:
                feats = rgbs.unsqueeze(0)

            rgb, alpha = rasterize_to_pixels(
                self.xys.unsqueeze(0),
                conics,
                feats,
                opacities.squeeze(-1),
                W,
                H,
                tile_size,
                isect_offsets,
                gauss_ids,
            )

            rgb = rgb.squeeze(0)
            alpha = alpha.squeeze(0)
            if rgb.shape[-1] == 4:
                rgb, depth_im = rgb[..., :3], rgb[..., 3:]
                depth_im = torch.where(alpha > 0, depth_im / alpha, depth_im.detach().max())
            else:
                depth_im = None
            rgb = rgb + (1.0 - alpha) * background
            rgb = torch.clamp(rgb, max=1.0)  # type: ignore
        else:
            rgb, alpha = rasterize_gaussians(  # type: ignore
                self.xys,
                depths,
                self.radii,
                conics,
                num_tiles_hit,  # type: ignore
                rgbs,
                opacities,
                H,
                W,
                BLOCK_WIDTH,
                background=background,
                return_alpha=True,
            )  # type: ignore
            alpha = alpha[..., None]
            rgb = torch.clamp(rgb, max=1.0)  # type: ignore
            depth_im = None
            if self.config.output_depth_during_training or not self.training:
                depth_im = rasterize_gaussians(  # type: ignore
                    self.xys,
                    depths,
                    self.radii,
                    conics,
                    num_tiles_hit,  # type: ignore
                    depths[:, None].repeat(1, 3),
                    opacities,
                    H,
                    W,
                    BLOCK_WIDTH,
                    background=torch.zeros(3, device=device),
                )[
                    ..., 0:1
                ]  # type: ignore
                depth_im = torch.where(alpha > 0, depth_im / alpha, depth_im.detach().max())
        
        # first_image = rgb.clone().detach().cpu().numpy()
        # print("IMAGE SHAPE:", first_image.shape)

        # # Transpose the image if needed (e.g., if it is in CHW format)
        # first_image = np.transpose(first_image, (0, 1, 2))  # Adjust transpose order based on your needs

        # # Print transposed shape
        # print("TRANSPOSED:", first_image.shape)

        # # Plot the transposed image
        # plt.imshow(first_image)
        # plt.axis('off')  # Turn off axis numbers and ticks
        # plt.savefig('test.png', bbox_inches='tight', pad_inches=0)


        return {"rgb": rgb, "depth": depth_im, "accumulation": alpha, "background": background}  # type: ignore

    def get_gt_img(self, image: torch.Tensor):
        """Compute groundtruth image with iteration dependent downscale factor for evaluation purpose

        Args:
            image: tensor.Tensor in type uint8 or float32
        """
        if image.dtype == torch.uint8:
            image = image.float() / 255.0
        gt_img = self._downscale_if_required(image)
        return gt_img

    def composite_with_background(self, image, background) -> torch.Tensor:
        """Composite the ground truth image with a background color when it has an alpha channel.

        Args:
            image: the image to composite
            background: the background color
        """
        if image.shape[2] == 4:
            alpha = image[..., -1].unsqueeze(-1).repeat((1, 1, 3))
            return alpha * image[..., :3] + (1 - alpha) * background
        else:
            return image

    def get_metrics_dict(self, outputs, batch) -> Dict[str, torch.Tensor]:
        """Compute and returns metrics.

        Args:
            outputs: the output to compute loss dict to
            batch: ground truth batch corresponding to outputs
        """
        gt_rgb = self.composite_with_background(
            self.get_gt_img(batch["image"]), outputs["background"]
        )
        metrics_dict = {}
        predicted_rgb = outputs["rgb"]
        metrics_dict["psnr"] = self.psnr(predicted_rgb, gt_rgb)

        metrics_dict["gaussian_count"] = self.num_points
        return metrics_dict

    def get_loss_dict(
        self, outputs, batch, metrics_dict=None
    ) -> Dict[str, torch.Tensor]:
        """Computes and returns the losses dict.

        Args:
            outputs: the output to compute loss dict to
            batch: ground truth batch corresponding to outputs
            metrics_dict: dictionary of metrics, some of which we can use for loss
        """
        gt_img = self.composite_with_background(
            self.get_gt_img(batch["image"]), outputs["background"]
        )
        pred_img = outputs["rgb"]
        device = gt_img.device

        # Set masked part of both ground-truth and rendered image to black.
        # This is a little bit sketchy for the SSIM loss.
        if "mask" in batch:
            # batch["mask"] : [H, W, 1]
            mask = self._downscale_if_required(batch["mask"])
            mask = mask.to(device)
            assert mask.shape[:2] == gt_img.shape[:2] == pred_img.shape[:2]
            gt_img = gt_img * mask
            pred_img = pred_img * mask

        Ll1 = torch.abs(gt_img - pred_img).mean()
        simloss = 1 - self.ssim(
            gt_img.permute(2, 0, 1)[None, ...], pred_img.permute(2, 0, 1)[None, ...]
        )
        if self.config.use_scale_regularization and self.step % 10 == 0:
            scale_exp = torch.exp(self.scales)
            scale_reg = (
                torch.maximum(
                    scale_exp.amax(dim=-1) / scale_exp.amin(dim=-1),
                    torch.tensor(self.config.max_gauss_ratio),
                )
                - self.config.max_gauss_ratio
            )
            scale_reg = 0.1 * scale_reg.mean()
        else:
            scale_reg = torch.tensor(0.0).to(device)

        return {
            "main_loss": (1 - self.config.ssim_lambda) * Ll1
            + self.config.ssim_lambda * simloss,
            "scale_reg": scale_reg,
        }

    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:
        """Writes the test image outputs.

        Args:
            image_idx: Index of the image.
            step: Current step.
            batch: Batch of data.
            outputs: Outputs of the model.

        Returns:
            A dictionary of metrics.
        """
        gt_rgb = self.composite_with_background(
            self.get_gt_img(batch["image"]), outputs["background"]
        )
        d = self._get_downscale_factor()
        if d > 1:
            # torchvision can be slow to import, so we do it lazily.
            import torchvision.transforms.functional as TF

            newsize = [batch["image"].shape[0] // d, batch["image"].shape[1] // d]
            predicted_rgb = TF.resize(
                outputs["rgb"].permute(2, 0, 1), newsize, antialias=None
            ).permute(1, 2, 0)
        else:
            predicted_rgb = outputs["rgb"]

        combined_rgb = torch.cat([gt_rgb, predicted_rgb], dim=1)

        # Switch images from [H, W, C] to [1, C, H, W] for metrics computations
        gt_rgb = torch.moveaxis(gt_rgb, -1, 0)[None, ...]
        predicted_rgb = torch.moveaxis(predicted_rgb, -1, 0)[None, ...]

        psnr = self.psnr(gt_rgb, predicted_rgb)
        ssim = self.ssim(gt_rgb, predicted_rgb)
        lpips = self.lpips(gt_rgb, predicted_rgb)

        # all of these metrics will be logged as scalars
        metrics_dict = {"psnr": float(psnr.item()), "ssim": float(ssim)}  # type: ignore
        metrics_dict["lpips"] = float(lpips)

        images_dict = {"img": combined_rgb}

        return metrics_dict, images_dict


def load_images_from_folder(folder_path: str) -> List[np.ndarray]:
    """
    Load all images from a folder into a list.

    Args:
        folder_path (str): Path to the folder containing images.

    Returns:
        List[np.ndarray]: List of images as NumPy arrays.
    """
    image_files = sorted(
        [f for f in os.listdir(folder_path) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.JPG'))]
    )
    images = [imageio.imread(os.path.join(folder_path, file)) for file in image_files]
    return images

def trainval(args):
    device = "cuda:0"
    torch.manual_seed(42)
    np.random.seed(42)
    os.makedirs("results", exist_ok=True)

    DATA_PATH = "/home/ubuntu/workdir/hariharan/DATA/alti/"
    images_folder = os.path.join(DATA_PATH, "images")

    images = load_images_from_folder(images_folder)
    images = np.stack([img[..., :3] for img in images])
    images = torch.from_numpy(images).float().to(device) / 255.0

    print(f"Loaded {images.shape[0]} images from {images_folder}")

    c2ws = torch.eye(4).repeat(len(images), 1, 1).to(device) 
    K = torch.tensor([[320.0, 0.0, 320.0], [0.0, 320.0, 320.0], [0.0, 0.0, 1.0]]).to(device)  # Dummy data

    points = torch.rand(5000, 3).float().to(device)  # Dummy data
    points_rgb = torch.randint(0, 255, (5000, 3)).float().to(device)  # Dummy data

    n_images = len(images)
    height, width = images.shape[1:3]

    # splits
    indices_train = [i for i in range(n_images) if i % 8 != 0]
    indices_val = [i for i in range(n_images) if i % 8 == 0]

    # model
    config = SplatfactoModelConfig()

    print(args.bigmodel)


    if args.bigmodel:
        config.cull_alpha_thresh = 0.005
        config.continue_cull_post_densification = False
    model = SplatfactoModel(
        config=config,
        num_train_data=len(indices_train),
        seed_points=(points, points_rgb),
    ).to(device)

    # optimizers
    optimizers = {
        "means": torch.optim.Adam([model.gauss_params["means"]], lr=1.6e-6, eps=1e-15),
        "features_dc": torch.optim.Adam(
            [model.gauss_params["features_dc"]], lr=0.0025, eps=1e-15
        ),
        "features_rest": torch.optim.Adam(
            [model.gauss_params["features_rest"]], lr=0.0025 / 20, eps=1e-15
        ),
        "opacities": torch.optim.Adam(
            [model.gauss_params["opacities"]], lr=0.05, eps=1e-15
        ),
        "scales": torch.optim.Adam([model.gauss_params["scales"]], lr=0.005, eps=1e-15),
        "quats": torch.optim.Adam([model.gauss_params["quats"]], lr=0.001, eps=1e-15),
    }

    # train
    pbar = tqdm.trange(args.steps)
    for step in pbar:
        model.train()
        model.train_callback_before_iteration(step)

        index = np.random.choice(indices_train)

        outputs = model.get_outputs(c2ws[index], K, width, height)
        loss_dict = model.get_loss_dict(outputs, {"image": images[index]})
        loss = loss_dict["main_loss"] + loss_dict["scale_reg"]

        for optimizer in optimizers.values():
            optimizer.zero_grad()
        loss.backward()
        for optimizer in optimizers.values():
            optimizer.step()
        pbar.set_description(f"Loss: {loss_dict['main_loss'].item():.3f}")
        
        if step % 500 == 0:
            print("INDEX : ", index)
            colors = outputs["rgb"]
            pixels = model.get_gt_img(images[index])
            loss = ((pixels - colors) ** 2).mean()
            psnr = -10.0 * torch.log(loss) / np.log(10.0)

            canvas = torch.vstack([pixels, colors])
            canvas = (canvas * 255.0).detach().cpu().numpy().astype(np.uint8)
            imageio.imwrite(f"results/images/image_{step}.png", canvas)

        model.train_callback_after_iteration(optimizers, step)
    print("INDEX : ", index)

    print(optimizers["opacities"])

    print(model.gauss_params["opacities"])

    npz_path = os.path.join("DATA", "new.npz")
    np.savez_compressed(
        npz_path,
        height=height,
        width=width,
        viewmats=c2ws.cpu().numpy(),
        Ks=K.cpu().numpy(),
        means3d=model.gauss_params["means"].detach().cpu().numpy(),
        scales=model.gauss_params["scales"].detach().cpu().numpy(),
        quats=model.gauss_params["quats"].detach().cpu().numpy(),
        opacities=model.gauss_params["opacities"].detach().cpu().numpy(),
        colors=model.gauss_params["features_dc"].detach().cpu().numpy(),
        features_rest=model.gauss_params["features_rest"].detach().cpu().numpy(),
    )

    model.eval()
    metrics_dict_list = []
    eval_time = 0
    for index in indices_val:
        with torch.no_grad():
            torch.cuda.synchronize()
            tic = time.time()
            outputs = model.get_outputs(c2ws[index], K, width, height)
            torch.cuda.synchronize()
            eval_time += time.time() - tic
            metrics_dict, images_dict = model.get_image_metrics_and_images(
                outputs, {"image": images[index]}
            )
            metrics_dict_list.append(metrics_dict)
    if len(metrics_dict_list) > 0:
        keys = metrics_dict_list[0].keys()
        metrics_dict = {
            key: np.mean([d[key] for d in metrics_dict_list]) for key in keys
        }
        print(f"Eval metrics on {len(metrics_dict_list)} images", metrics_dict)
        print(f"Eval time: {eval_time:.4f}s")
    else:
        print("No validation images")

    outputs_test = model.get_outputs(c2ws[0], K, width, height)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scene", type=str, default="garden", help="Mip-NeRF 360 scene name"
    )
    parser.add_argument(
        "--steps", type=int, default=7000, help="Number of steps to train"
    )
    parser.add_argument("--bigmodel", action="store_true", help="Use a bigger model")
    args = parser.parse_args()

    trainval(args)
