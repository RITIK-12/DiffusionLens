# Copyright 2023 The HuggingFace Team. All rights reserved.
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

import inspect
import warnings
from typing import Any, Callable, Dict, List, Optional, Union

import torch
from packaging import version
from transformers import CLIPImageProcessor, CLIPTextModel, CLIPTokenizer

from ...configuration_utils import FrozenDict
from ...image_processor import VaeImageProcessor
from ...loaders import FromSingleFileMixin, LoraLoaderMixin, TextualInversionLoaderMixin
from ...models import AutoencoderKL, UNet2DConditionModel
from ...schedulers import KarrasDiffusionSchedulers
from ...utils import (
    deprecate,
    is_accelerate_available,
    is_accelerate_version,
    logging,
    randn_tensor,
    replace_example_docstring,
)
from ..pipeline_utils import DiffusionPipeline
from . import StableDiffusionPipelineOutput
from .safety_checker import StableDiffusionSafetyChecker
import os
import matplotlib.pyplot as plt
from baukit import TraceDict

def make_grid(images, nrow=2):
    plt.figure(figsize=(20, 20))
    for index, image in enumerate(images):
        plt.subplot(nrow, nrow, index+1)
        plt.imshow(image)
        plt.axis('off')
    return plt


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> import torch
        >>> from diffusers import StableDiffusionPipeline

        >>> pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16)
        >>> pipe = pipe.to("cuda")

        >>> prompt = "a photo of an astronaut riding a horse on mars"
        >>> image = pipe(prompt).images[0]
        ```
"""


def rescale_noise_cfg(noise_cfg, noise_pred_text, guidance_rescale=0.0):
    """
    Rescale `noise_cfg` according to `guidance_rescale`. Based on findings of [Common Diffusion Noise Schedules and
    Sample Steps are Flawed](https://arxiv.org/pdf/2305.08891.pdf). See Section 3.4
    """
    std_text = noise_pred_text.std(dim=list(range(1, noise_pred_text.ndim)), keepdim=True)
    std_cfg = noise_cfg.std(dim=list(range(1, noise_cfg.ndim)), keepdim=True)
    # rescale the results from guidance (fixes overexposure)
    noise_pred_rescaled = noise_cfg * (std_text / std_cfg)
    # mix with the original results from guidance by factor guidance_rescale to avoid "plain looking" images
    noise_cfg = guidance_rescale * noise_pred_rescaled + (1 - guidance_rescale) * noise_cfg
    return noise_cfg


class StableDiffusionPipeline(DiffusionPipeline, TextualInversionLoaderMixin, LoraLoaderMixin, FromSingleFileMixin):
    r"""
    Pipeline for text-to-image generation using Stable Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    The pipeline also inherits the following loading methods:
        - [`~loaders.TextualInversionLoaderMixin.load_textual_inversion`] for loading textual inversion embeddings
        - [`~loaders.LoraLoaderMixin.load_lora_weights`] for loading LoRA weights
        - [`~loaders.LoraLoaderMixin.save_lora_weights`] for saving LoRA weights
        - [`~loaders.FromSingleFileMixin.from_single_file`] for loading `.ckpt` files

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) model to encode and decode images to and from latent representations.
        text_encoder ([`~transformers.CLIPTextModel`]):
            Frozen text-encoder ([clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14)).
        tokenizer ([`~transformers.CLIPTokenizer`]):
            A `CLIPTokenizer` to tokenize text.
        unet ([`UNet2DConditionModel`]):
            A `UNet2DConditionModel` to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
        safety_checker ([`StableDiffusionSafetyChecker`]):
            Classification module that estimates whether generated images could be considered offensive or harmful.
            Please refer to the [model card](https://huggingface.co/runwayml/stable-diffusion-v1-5) for more details
            about a model's potential harms.
        feature_extractor ([`~transformers.CLIPImageProcessor`]):
            A `CLIPImageProcessor` to extract features from generated images; used as inputs to the `safety_checker`.
    """
    _optional_components = ["safety_checker", "feature_extractor"]

    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet2DConditionModel,
        scheduler: KarrasDiffusionSchedulers,
        safety_checker: StableDiffusionSafetyChecker,
        feature_extractor: CLIPImageProcessor,
        requires_safety_checker: bool = True,
    ):
        super().__init__()

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file"
            )
            deprecate("steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        if hasattr(scheduler.config, "clip_sample") and scheduler.config.clip_sample is True:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} has not set the configuration `clip_sample`."
                " `clip_sample` should be set to False in the configuration file. Please make sure to update the"
                " config accordingly as not setting `clip_sample` in the config might lead to incorrect results in"
                " future versions. If you have downloaded this checkpoint from the Hugging Face Hub, it would be very"
                " nice if you could open a Pull request for the `scheduler/scheduler_config.json` file"
            )
            deprecate("clip_sample not set", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["clip_sample"] = False
            scheduler._internal_dict = FrozenDict(new_config)

        if safety_checker is None and requires_safety_checker:
            logger.warning(
                f"You have disabled the safety checker for {self.__class__} by passing `safety_checker=None`. Ensure"
                " that you abide to the conditions of the Stable Diffusion license and do not expose unfiltered"
                " results in services or applications open to the public. Both the diffusers team and Hugging Face"
                " strongly recommend to keep the safety filter enabled in all public facing circumstances, disabling"
                " it only for use-cases that involve analyzing network behavior or auditing its results. For more"
                " information, please have a look at https://github.com/huggingface/diffusers/pull/254 ."
            )

        if safety_checker is not None and feature_extractor is None:
            raise ValueError(
                "Make sure to define a feature extractor when loading {self.__class__} if you want to use the safety"
                " checker. If you do not want to use the safety checker, you can pass `'safety_checker=None'` instead."
            )

        is_unet_version_less_0_9_0 = hasattr(unet.config, "_diffusers_version") and version.parse(
            version.parse(unet.config._diffusers_version).base_version
        ) < version.parse("0.9.0.dev0")
        is_unet_sample_size_less_64 = hasattr(unet.config, "sample_size") and unet.config.sample_size < 64
        if is_unet_version_less_0_9_0 and is_unet_sample_size_less_64:
            deprecation_message = (
                "The configuration file of the unet has set the default `sample_size` to smaller than"
                " 64 which seems highly unlikely. If your checkpoint is a fine-tuned version of any of the"
                " following: \n- CompVis/stable-diffusion-v1-4 \n- CompVis/stable-diffusion-v1-3 \n-"
                " CompVis/stable-diffusion-v1-2 \n- CompVis/stable-diffusion-v1-1 \n- runwayml/stable-diffusion-v1-5"
                " \n- runwayml/stable-diffusion-inpainting \n you should change 'sample_size' to 64 in the"
                " configuration file. Please make sure to update the config accordingly as leaving `sample_size=32`"
                " in the config might lead to incorrect results in future versions. If you have downloaded this"
                " checkpoint from the Hugging Face Hub, it would be very nice if you could open a Pull request for"
                " the `unet/config.json` file"
            )
            deprecate("sample_size<64", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(unet.config)
            new_config["sample_size"] = 64
            unet._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor,
        )
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(vae_scale_factor=self.vae_scale_factor)
        self.register_to_config(requires_safety_checker=requires_safety_checker)

    def enable_vae_slicing(self):
        r"""
        Enable sliced VAE decoding. When this option is enabled, the VAE will split the input tensor in slices to
        compute decoding in several steps. This is useful to save some memory and allow larger batch sizes.
        """
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        r"""
        Disable sliced VAE decoding. If `enable_vae_slicing` was previously enabled, this method will go back to
        computing decoding in one step.
        """
        self.vae.disable_slicing()

    def enable_vae_tiling(self):
        r"""
        Enable tiled VAE decoding. When this option is enabled, the VAE will split the input tensor into tiles to
        compute decoding and encoding in several steps. This is useful for saving a large amount of memory and to allow
        processing larger images.
        """
        self.vae.enable_tiling()

    def disable_vae_tiling(self):
        r"""
        Disable tiled VAE decoding. If `enable_vae_tiling` was previously enabled, this method will go back to
        computing decoding in one step.
        """
        self.vae.disable_tiling()

    def enable_model_cpu_offload(self, gpu_id=0):
        r"""
        Offload all models to CPU to reduce memory usage with a low impact on performance. Moves one whole model at a
        time to the GPU when its `forward` method is called, and the model remains in GPU until the next model runs.
        Memory savings are lower than using `enable_sequential_cpu_offload`, but performance is much better due to the
        iterative execution of the `unet`.
        """
        if is_accelerate_available() and is_accelerate_version(">=", "0.17.0.dev0"):
            from accelerate import cpu_offload_with_hook
        else:
            raise ImportError("`enable_model_cpu_offload` requires `accelerate v0.17.0` or higher.")

        device = torch.device(f"cuda:{gpu_id}")

        if self.device.type != "cpu":
            self.to("cpu", silence_dtype_warnings=True)
            torch.cuda.empty_cache()  # otherwise we don't see the memory savings (but they probably exist)

        hook = None
        for cpu_offloaded_model in [self.text_encoder, self.unet, self.vae]:
            _, hook = cpu_offload_with_hook(cpu_offloaded_model, device, prev_module_hook=hook)

        if self.safety_checker is not None:
            _, hook = cpu_offload_with_hook(self.safety_checker, device, prev_module_hook=hook)

        # We'll offload the last model manually.
        self.final_offload_hook = hook

    def _encode_prompt(
        self,
        prompt,
        device,
        num_images_per_prompt,
        do_classifier_free_guidance,
        negative_prompt=None,
        prompt_embeds: Optional[torch.FloatTensor] = None,
        negative_prompt_embeds: Optional[torch.FloatTensor] = None,
        lora_scale: Optional[float] = None,
        skip_layers: Optional[list] = [None],
        # hidden_state_index: Optional[int] = 23,
        start_layer: Optional[int] = 0,
        end_layer: Optional[int] = None,
        step_layer: Optional[int] = 1,
        explain_other_model: Optional[bool] = False,
        per_token: Optional[bool] = False,
    ):
        r"""
        Encodes the prompt into text encoder hidden states.

        Args:
             prompt (`str` or `List[str]`, *optional*):
                prompt to be encoded
            device: (`torch.device`):
                torch device
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            lora_scale (`float`, *optional*):
                A lora scale that will be applied to all LoRA layers of the text encoder if LoRA layers are loaded.
        """
        # set lora scale so that monkey patched LoRA
        # function of text encoder can correctly access it

        # print model info
        # model_info(self.text_encoder)

        if lora_scale is not None and isinstance(self, LoraLoaderMixin):
            self._lora_scale = lora_scale

        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]

        if prompt_embeds is None:
            # print(f"Encoding prompt...HS: {hidden_state_index}")
            # textual inversion: procecss multi-vector tokens if necessary
            if isinstance(self, TextualInversionLoaderMixin):
                prompt = self.maybe_convert_prompt(prompt, self.tokenizer)

            text_inputs = self.tokenizer(
                prompt,
                padding="max_length",
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors="pt",
            )
            text_input_ids = text_inputs.input_ids
            untruncated_ids = self.tokenizer(prompt, padding="longest", return_tensors="pt").input_ids

            if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(
                text_input_ids, untruncated_ids
            ):
                removed_text = self.tokenizer.batch_decode(
                    untruncated_ids[:, self.tokenizer.model_max_length - 1 : -1]
                )
                logger.warning(
                    "The following part of your input was truncated because CLIP can only handle sequences up to"
                    f" {self.tokenizer.model_max_length} tokens: {removed_text}"
                )

            if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
                attention_mask = text_inputs.attention_mask.to(device)
            else:
                attention_mask = None

            # Diffusion lens updated code - Toker
            output = self.text_encoder(
                text_input_ids.to(device),
                attention_mask=attention_mask,
                output_hidden_states=True,
                return_dict=True,
            )
            explain_gpt = False
            explain_pythia = True
            use_transformation = False
            # from pythia_adapt.layers_map import load_encoders
            if explain_other_model:
                # load gpt model for the lm head
                if explain_gpt:
                    from transformers import GPT2Tokenizer, GPT2Model
                    tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
                    gpt = GPT2Model.from_pretrained('gpt2', torch_dtype=torch.bfloat16)
                    gpt = gpt.to(device)
                    output = gpt(text_input_ids.to(device), attention_mask=attention_mask,
                                 output_hidden_states=True, return_dict=True)
                    decoded_output = tokenizer.decode(text_input_ids[0])
                    lm_head = gpt.lm_head
                    embedding_matrix = gpt.transformer.wte.weight.data
                    lm_head = lm_head.half().to(device)
                    embedding_matrix = embedding_matrix.half().to(device)


                if explain_pythia:
                    print("Using pythia model for explanation...")
                    from transformers import AutoTokenizer, AutoModelForCausalLM

                    import sys
                    # caution: path[0] is reserved for script path (or '' in REPL)
                    sys.path.insert(1, '/home/tok/IF/diffusers_local/src/diffusers/pipelines/stable_diffusion/pythia_adapt')
                    # import load_encoders from layers_map.py in pythia_adapt
                    from layers_map import load_encoders
                    # from pythia_adapt.layers_map import load_encoders
                    pythia_tokenizer = AutoTokenizer.from_pretrained("EleutherAI/pythia-410m")
                    pythia_tokenizer.add_special_tokens({'pad_token': '[PAD]'})
                    pythia_model = AutoModelForCausalLM.from_pretrained("EleutherAI/pythia-410m",
                                                                       torch_dtype=torch.float16).to(device)

                    empty_prompt = 'dog' + self.tokenizer.pad_token * (77 - 1) # self.tokenizer.pad_token * 77 #
                    empty_text_inputs = self.tokenizer(
                        empty_prompt,
                        padding="max_length",
                        max_length=self.tokenizer.model_max_length,
                        truncation=True,
                        return_tensors="pt",
                    )

                    empty_text_attention_mask = empty_text_inputs.attention_mask.to(device)
                    empty_text_input_ids = empty_text_inputs.input_ids

                    output = self.text_encoder(
                        empty_text_input_ids.to(device),
                        attention_mask=empty_text_attention_mask,
                        output_hidden_states=True,
                        return_dict=True,
                    )

                    if use_transformation:
                        text_input_ids = pythia_tokenizer(prompt,
                                                          # padding="max_length",
                                         # max_length=77,
                                         truncation=True, return_tensors="pt").input_ids

                        pythia_output = pythia_model(text_input_ids.to(device),
                                                  output_hidden_states=True,
                                              return_dict=True)
                    else:
                        # # todo remove: just a check...
                        text_input_ids = self.tokenizer(
                            prompt,
                            truncation=True,
                            return_tensors="pt",
                        )

                        attention_mask = text_input_ids.attention_mask.to(device)
                        text_input_ids = text_input_ids.input_ids

                        pythia_output = self.text_encoder(
                            text_input_ids.to(device),
                            output_hidden_states=True,
                            attention_mask=attention_mask,
                            return_dict=True,
                        )

                    #.hidden_states[-1][:, -1]
                    num_layers = len(pythia_output.hidden_states) - 1 # TODO remove -1
                    hidden_size = len(pythia_output.hidden_states[0][0][0])
                    print(f'num_layers: {num_layers}, hidden_size: {hidden_size}')
                    encoders = load_encoders(num_layers, hidden_size)
                    # translate each layer in the output via the corresponding encoder
                    for layer_index, (encoder_name, encoder) in enumerate(encoders.items()):
                        encoder = encoder.to(device)
                        print(f'output.hidden_states[layer_index] shape: {pythia_output.hidden_states[layer_index].shape}')
                        pythia_hidden_states = pythia_output.hidden_states[layer_index][0]
                        print(f'hidden_states shape: {pythia_hidden_states.shape}')
                        for token_index, token_representation in enumerate(pythia_hidden_states):
                            print("token_index:", token_index)
                            # if it's not the last token in the sequence - skip it
                            # if token_index < pythia_hidden_states.shape[0] - 1:
                            #     continue
                            # if token_index > pythia_hidden_states.shape[0] - 1:
                            #     break
                            # continue
                            print(f'token_representation shape: {token_representation.shape}')
                            print(f'Changing token: {token_index}')
                            token_representation = token_representation.float()
                            if use_transformation:
                                transformed_hs = encoder.get_features(token_representation)
                            else:
                                transformed_hs = token_representation
                            print(f'transformed_hs shape: {transformed_hs.shape}')
                            output.hidden_states[layer_index][0][token_index] = transformed_hs


                # from transformers import T5Tokenizer, T5ForConditionalGeneration, AutoTokenizer
                # model_name = 'google/t5-v1_1-xxl'
                # t5_model = T5ForConditionalGeneration.from_pretrained(model_name, torch_dtype=torch.bfloat16)
                # t5_model = t5_model.to(device)
                # lm_head = t5_model.lm_head
                # lm_head = lm_head.half().to(device)
                # embedding_matrix = t5_model.encoder.embed_tokens.weight.data
                # embedding_matrix = embedding_matrix.half().to(device)

            # prompt_embeds_prev = self.text_encoder(
            #     text_input_ids.to(device),
            #     attention_mask=attention_mask,
            # )


            if skip_layers is not None and skip_layers[0] is not None:
                embeds = output.hidden_states[skip_layers[0] - 1]
                def patch_rep(x, layer):
                    # x[0].shape = torch.Size([4, 77, 4096])
                    # x[1].shape = torch.Size([4, 64, 77, 77])
                    # print("embeds shape:", embeds.shape)
                    print("In patch_rep")
                    print(f'layer: {layer}')

                    # for index in range(77):
                    #     x[0][0, index] = embeds[0, index].clone()
                    # do it in one line
                    x[0][0] = embeds[0].clone()
                    return x

                embed_layername = f'text_model.encoder.layers.{skip_layers[0]}'
                print(f'embed_layername: {embed_layername}')
                print("=" * 60)
                with torch.no_grad(), TraceDict(
                        self.text_encoder,
                        [embed_layername],
                        edit_output=patch_rep,
                ) as td:
                    outputs_exp = self.text_encoder(
                        input_ids=text_input_ids.to(self.device),
                        attention_mask=text_inputs.attention_mask.to(self.device),
                        output_hidden_states=True,
                        return_dict=True,
                    )
                all_embeds_with_skip = outputs_exp['hidden_states'] # TODO [skip_layers[0] - 1:]
                last_hidden_state_with_skip = outputs_exp['last_hidden_state'].detach()

            final_layer_norm = self.text_encoder.text_model.final_layer_norm
            hidden_states = output.hidden_states

        else:
            print("Using provided prompt embeddings...")

        prompt_embeds_per_layer = []

        analysis_file_name = os.path.join('generations', 'stats', prompt, 'sd2.1', 'analysis.txt')
        if not os.path.exists(os.path.dirname(analysis_file_name)):
            os.makedirs(os.path.dirname(analysis_file_name))

        analysis_file = open(analysis_file_name, 'w')

        def calculate_stats(title, vec, file):
            # print into the analysis file
            file.write(title)
            file.write(f'full representation: {vec}\n\n')
            # norm
            prompt_embeds_norm = torch.norm(vec, dim=-1)
            file.write(f'norm: {prompt_embeds_norm}\n\n')
            # mean
            prompt_embeds_mean = torch.mean(vec, dim=-1)
            file.write(f'mean: {prompt_embeds_mean}\n\n')
            file.write(f'mean: {torch.mean(vec)}, std: {torch.std(vec)}, max: {torch.max(vec)}, min: {torch.min(vec)}\n\n')

        if per_token:
            pad_prompt = self.tokenizer.pad_token * 77
            pad_text_inputs = self.tokenizer(
                pad_prompt,
                padding="max_length",
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors="pt",
            )

            pad_text_attention_mask = pad_text_inputs.attention_mask.to(device)
            pad_text_input_ids = pad_text_inputs.input_ids

            pad_output = self.text_encoder(
                pad_text_input_ids.to(device),
                attention_mask=pad_text_attention_mask,
                output_hidden_states=True,
                return_dict=True,
            )

        token_index_counter = 0
        for token_index in range(len(text_input_ids[0])):
            if token_index_counter > 0 and not per_token:
                break

            for layer_index, prompt_embeds in enumerate(hidden_states[start_layer:end_layer:step_layer]):
                # if layer_index == 0:
                #     continue
                # prompt_embeds = prompt_embeds - hidden_states[layer_index - 1]

                if per_token:
                    # replace all but the current token with pad tokens from pad_output
                    pad_output.hidden_states[layer_index][0][:token_index] = (
                        (pad_output.hidden_states[layer_index][0][:token_index]).clone()
                    )
                    pad_output.hidden_states[layer_index][0][token_index + 1:] = (
                        (pad_output.hidden_states[layer_index][0][token_index + 1:]).clone()
                    )

                # TODO get back if needed
                negative_prompt_embeds = None
                if not (start_layer + layer_index * step_layer == len(hidden_states) - 1):
                    print(f"layer: {start_layer + layer_index * step_layer} - using final_layer_norm")
                    prompt_embeds = final_layer_norm(prompt_embeds)
                else:
                    print(f"layer: {start_layer + layer_index * step_layer} - still using final_layer_norm")
                    prompt_embeds = final_layer_norm(prompt_embeds)

                prompt_embeds = prompt_embeds.to(dtype=self.text_encoder.dtype, device=device)

                bs_embed, seq_len, _ = prompt_embeds.shape
                # duplicate text embeddings for each generation per prompt, using mps friendly method
                prompt_embeds = prompt_embeds.repeat(1, num_images_per_prompt, 1)
                prompt_embeds = prompt_embeds.view(bs_embed * num_images_per_prompt, seq_len, -1)

                # get unconditional embeddings for classifier free guidance
                if do_classifier_free_guidance and negative_prompt_embeds is None:
                    uncond_tokens: List[str]
                    if negative_prompt is None:
                        uncond_tokens = [""] * batch_size
                    elif prompt is not None and type(prompt) is not type(negative_prompt):
                        raise TypeError(
                            f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
                            f" {type(prompt)}."
                        )
                    elif isinstance(negative_prompt, str):
                        uncond_tokens = [negative_prompt]
                    elif batch_size != len(negative_prompt):
                        raise ValueError(
                            f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
                            f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
                            " the batch size of `prompt`."
                        )
                    else:
                        uncond_tokens = negative_prompt

                    # textual inversion: procecss multi-vector tokens if necessary
                    if isinstance(self, TextualInversionLoaderMixin):
                        uncond_tokens = self.maybe_convert_prompt(uncond_tokens, self.tokenizer)

                    max_length = prompt_embeds.shape[1]
                    uncond_input = self.tokenizer(
                        uncond_tokens,
                        padding="max_length",
                        max_length=max_length,
                        truncation=True,
                        return_tensors="pt",
                    )

                    if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
                        attention_mask = uncond_input.attention_mask.to(device)
                    else:
                        attention_mask = None

                    negative_prompt_embeds = self.text_encoder(
                        uncond_input.input_ids.to(device),
                        attention_mask=attention_mask,
                    )
                    negative_prompt_embeds = negative_prompt_embeds[0]

                if do_classifier_free_guidance:
                    # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
                    seq_len = negative_prompt_embeds.shape[1]

                    negative_prompt_embeds = negative_prompt_embeds.to(dtype=self.text_encoder.dtype, device=device)

                    negative_prompt_embeds = negative_prompt_embeds.repeat(1, num_images_per_prompt, 1)
                    negative_prompt_embeds = negative_prompt_embeds.view(batch_size * num_images_per_prompt, seq_len, -1)

                    # For classifier free guidance, we need to do two forward passes.
                    # Here we concatenate the unconditional and text embeddings into a single batch
                    # to avoid doing two forward passes

                    prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds])
                    prompt_embeds_per_layer.append(prompt_embeds)
            token_index_counter += 1
        analysis_file.close()
        return prompt_embeds_per_layer

    def run_safety_checker(self, image, device, dtype):
        if self.safety_checker is None:
            has_nsfw_concept = None
        else:
            if torch.is_tensor(image):
                feature_extractor_input = self.image_processor.postprocess(image, output_type="pil")
            else:
                feature_extractor_input = self.image_processor.numpy_to_pil(image)
            safety_checker_input = self.feature_extractor(feature_extractor_input, return_tensors="pt").to(device)
            image, has_nsfw_concept = image, [False] * image.shape[0]
                # self.safety_checker(
                # images=image, clip_input=safety_checker_input.pixel_values.to(dtype)
            # )
        return image, has_nsfw_concept

    def decode_latents(self, latents):
        warnings.warn(
            "The decode_latents method is deprecated and will be removed in a future version. Please"
            " use VaeImageProcessor instead",
            FutureWarning,
        )
        latents = 1 / self.vae.config.scaling_factor * latents
        image = self.vae.decode(latents, return_dict=False)[0]
        image = (image / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
        image = image.cpu().permute(0, 2, 3, 1).float().numpy()
        return image

    def prepare_extra_step_kwargs(self, generator, eta):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]

        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        # check if the scheduler accepts generator
        accepts_generator = "generator" in set(inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(
        self,
        prompt,
        height,
        width,
        callback_steps,
        negative_prompt=None,
        prompt_embeds=None,
        negative_prompt_embeds=None,
    ):
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        if (callback_steps is None) or (
            callback_steps is not None and (not isinstance(callback_steps, int) or callback_steps <= 0)
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )

        if prompt is not None and prompt_embeds is not None:
            raise ValueError(
                f"Cannot forward both `prompt`: {prompt} and `prompt_embeds`: {prompt_embeds}. Please make sure to"
                " only forward one of the two."
            )
        elif prompt is None and prompt_embeds is None:
            raise ValueError(
                "Provide either `prompt` or `prompt_embeds`. Cannot leave both `prompt` and `prompt_embeds` undefined."
            )
        elif prompt is not None and (not isinstance(prompt, str) and not isinstance(prompt, list)):
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if negative_prompt is not None and negative_prompt_embeds is not None:
            raise ValueError(
                f"Cannot forward both `negative_prompt`: {negative_prompt} and `negative_prompt_embeds`:"
                f" {negative_prompt_embeds}. Please make sure to only forward one of the two."
            )

        if prompt_embeds is not None and negative_prompt_embeds is not None:
            if prompt_embeds.shape != negative_prompt_embeds.shape:
                raise ValueError(
                    "`prompt_embeds` and `negative_prompt_embeds` must have the same shape when passed directly, but"
                    f" got: `prompt_embeds` {prompt_embeds.shape} != `negative_prompt_embeds`"
                    f" {negative_prompt_embeds.shape}."
                )

    def prepare_latents(self, batch_size, num_channels_latents, height, width, dtype, device, generator, latents=None):
        shape = (batch_size, num_channels_latents, height // self.vae_scale_factor, width // self.vae_scale_factor)
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )

        if latents is None:
            latents = randn_tensor(shape, generator=generator, device=device, dtype=dtype)
        else:
            latents = latents.to(device)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    @torch.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
        self,
        prompt: Union[str, List[str]] = None,
        height: Optional[int] = None,
        width: Optional[int] = None,
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        num_images_per_prompt: Optional[int] = 1,
        eta: float = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        prompt_embeds: Optional[torch.FloatTensor] = None,
        negative_prompt_embeds: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: int = 1,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        guidance_rescale: float = 0.0,
        skip_layers: Optional[list] = None,
        start_layer: Optional[int] = 0,
        end_layer: Optional[int] = -1,
        step_layer: Optional[int] = 1,
        explain_other_model: Optional[bool] = False,
        per_token: Optional[bool] = False,
    ):
        r"""
        The call function to the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide image generation. If not defined, you need to pass `prompt_embeds`.
            height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                A higher guidance scale value encourages the model to generate images closely linked to the text
                `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide what to not include in image generation. If not defined, you need to
                pass `negative_prompt_embeds` instead. Ignored when not using guidance (`guidance_scale < 1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) from the [DDIM](https://arxiv.org/abs/2010.02502) paper. Only applies
                to the [`~schedulers.DDIMScheduler`], and is ignored in other schedulers.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
                generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`.
            prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs (prompt weighting). If not
                provided, text embeddings are generated from the `prompt` input argument.
            negative_prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs (prompt weighting). If
                not provided, `negative_prompt_embeds` are generated from the `negative_prompt` input argument.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generated image. Choose between `PIL.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that calls every `callback_steps` steps during inference. The function is called with the
                following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function is called. If not specified, the callback is called at
                every step.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the [`AttentionProcessor`] as defined in
                [`self.processor`](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).
            guidance_rescale (`float`, *optional*, defaults to 0.7):
                Guidance rescale factor from [Common Diffusion Noise Schedules and Sample Steps are
                Flawed](https://arxiv.org/pdf/2305.08891.pdf). Guidance rescale factor should fix overexposure when
                using zero terminal SNR.

        Examples:

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] is returned,
                otherwise a `tuple` is returned where the first element is a list with the generated images and the
                second element is a list of `bool`s indicating whether the corresponding generated image contains
                "not-safe-for-work" (nsfw) content.
        """
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(
            prompt, height, width, callback_steps, negative_prompt, prompt_embeds, negative_prompt_embeds
        )

        # 2. Define call parameters
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]

        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0

        # 3. Encode input prompt
        text_encoder_lora_scale = (
            cross_attention_kwargs.get("scale", None) if cross_attention_kwargs is not None else None
        )
        res = []

        prompt_embeds_per_layer = self._encode_prompt(
            prompt,
            device,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=None,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale,
            skip_layers=skip_layers,
            start_layer=start_layer,
            end_layer=end_layer,
            step_layer=step_layer,
            explain_other_model=explain_other_model,
            per_token=per_token
        )

        for hs_index, prompt_embeds in enumerate(prompt_embeds_per_layer):
            latents = None
            # 4. Prepare timesteps
            self.scheduler.set_timesteps(num_inference_steps, device=device)
            timesteps = self.scheduler.timesteps

            print(f'Timesteps: {timesteps}')

            # 5. Prepare latent variables
            num_channels_latents = self.unet.config.in_channels
            latents = self.prepare_latents(
                batch_size * num_images_per_prompt,
                num_channels_latents,
                height,
                width,
                prompt_embeds.dtype,
                device,
                generator,
                latents,
            )

            # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
            extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

            # 7. Denoising loop
            num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
            with self.progress_bar(total=num_inference_steps) as progress_bar:
                for i, t in enumerate(timesteps):
                    print(f'Iteration: {i}, t: {t}')
                    # expand the latents if we are doing classifier free guidance
                    latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                    latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)

                    # predict the noise residual
                    noise_pred = self.unet(
                        latent_model_input,
                        t,
                        encoder_hidden_states=prompt_embeds,
                        cross_attention_kwargs=cross_attention_kwargs,
                        return_dict=False,
                    )[0]

                    # perform guidance
                    if do_classifier_free_guidance:
                        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)


                    if do_classifier_free_guidance and guidance_rescale > 0.0:
                        # Based on 3.4. in https://arxiv.org/pdf/2305.08891.pdf
                        noise_pred = rescale_noise_cfg(noise_pred, noise_pred_text, guidance_rescale=guidance_rescale)

                    print(f'noise pred shape: {noise_pred.shape}')
                    # compute the previous noisy sample x_t -> x_t-1
                    latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs, return_dict=False)[0]

                    print(f'latents shape: {latents.shape}')

                    # call the callback, if provided
                    if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                        progress_bar.update()
                        if callback is not None and i % callback_steps == 0:
                            callback(i, t, latents)

            if not output_type == "latent":
                image = self.vae.decode(latents / self.vae.config.scaling_factor, return_dict=False)[0]
                image, has_nsfw_concept = self.run_safety_checker(image, device, prompt_embeds.dtype)
            else:
                image = latents
                has_nsfw_concept = None

            if has_nsfw_concept is None:
                do_denormalize = [True] * image.shape[0]
            else:
                do_denormalize = [not has_nsfw for has_nsfw in has_nsfw_concept]

            image = self.image_processor.postprocess(image,
                                                     output_type=output_type,
                                                     do_denormalize=do_denormalize)

            # Offload last model to CPU
            if hasattr(self, "final_offload_hook") and self.final_offload_hook is not None:
                self.final_offload_hook.offload()

            if not return_dict:
                return (image, has_nsfw_concept)

            # import os
            # output_folder = os.path.join('output_test', prompt)
            # if not os.path.exists(output_folder):
            #     os.makedirs(output_folder)
            #
            #
            # plot = (make_grid(image, nrow=2))
            # plot.savefig(f'{output_folder}/layer_{hs_index}.png')
            # plot.close()

            # reset attn weights for a new prompt
            # self.unet.zero_grad()
            res.append(StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept))

        return res

def model_info(model):
    print("=" * 20)
    print(model.named_modules())
    print("Config: ", model.config)
    print("=" * 20)
    print(model)
    print("=" * 20)
    layer_names = [name for name, _ in model.named_parameters()]
    # Print the layer names
    for name in layer_names:
        print(name)
    print(model)
# # Copyright 2023 The HuggingFace Team. All rights reserved.
# #
# # Licensed under the Apache License, Version 2.0 (the "License");
# # you may not use this file except in compliance with the License.
# # You may obtain a copy of the License at
# #
# #     http://www.apache.org/licenses/LICENSE-2.0
# #
# # Unless required by applicable law or agreed to in writing, software
# # distributed under the License is distributed on an "AS IS" BASIS,
# # WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# # See the License for the specific language governing permissions and
# # limitations under the License.
#
# import inspect
# import warnings
# from typing import Any, Callable, Dict, List, Optional, Union
#
# import torch
# from packaging import version
# from transformers import CLIPImageProcessor, CLIPTextModel, CLIPTokenizer
#
# from ...configuration_utils import FrozenDict
# from ...image_processor import VaeImageProcessor
# from ...loaders import FromSingleFileMixin, LoraLoaderMixin, TextualInversionLoaderMixin
# from ...models import AutoencoderKL, UNet2DConditionModel
# from ...schedulers import KarrasDiffusionSchedulers
# from ...utils import (
#     deprecate,
#     is_accelerate_available,
#     is_accelerate_version,
#     logging,
#     randn_tensor,
#     replace_example_docstring,
# )
# from ..pipeline_utils import DiffusionPipeline
# from . import StableDiffusionPipelineOutput
# from .safety_checker import StableDiffusionSafetyChecker
#
#
# logger = logging.get_logger(__name__)  # pylint: disable=invalid-name
#
# EXAMPLE_DOC_STRING = """
#     Examples:
#         ```py
#         >>> import torch
#         >>> from diffusers import StableDiffusionPipeline
#
#         >>> pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16)
#         >>> pipe = pipe.to("cuda")
#
#         >>> prompt = "a photo of an astronaut riding a horse on mars"
#         >>> image = pipe(prompt).images[0]
#         ```
# """
#
#
# def rescale_noise_cfg(noise_cfg, noise_pred_text, guidance_rescale=0.0):
#     """
#     Rescale `noise_cfg` according to `guidance_rescale`. Based on findings of [Common Diffusion Noise Schedules and
#     Sample Steps are Flawed](https://arxiv.org/pdf/2305.08891.pdf). See Section 3.4
#     """
#     std_text = noise_pred_text.std(dim=list(range(1, noise_pred_text.ndim)), keepdim=True)
#     std_cfg = noise_cfg.std(dim=list(range(1, noise_cfg.ndim)), keepdim=True)
#     # rescale the results from guidance (fixes overexposure)
#     noise_pred_rescaled = noise_cfg * (std_text / std_cfg)
#     # mix with the original results from guidance by factor guidance_rescale to avoid "plain looking" images
#     noise_cfg = guidance_rescale * noise_pred_rescaled + (1 - guidance_rescale) * noise_cfg
#     return noise_cfg
#
#
# class StableDiffusionPipeline(DiffusionPipeline, TextualInversionLoaderMixin, LoraLoaderMixin, FromSingleFileMixin):
#     r"""
#     Pipeline for text-to-image generation using Stable Diffusion.
#
#     This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
#     implemented for all pipelines (downloading, saving, running on a particular device, etc.).
#
#     The pipeline also inherits the following loading methods:
#         - [`~loaders.TextualInversionLoaderMixin.load_textual_inversion`] for loading textual inversion embeddings
#         - [`~loaders.LoraLoaderMixin.load_lora_weights`] for loading LoRA weights
#         - [`~loaders.LoraLoaderMixin.save_lora_weights`] for saving LoRA weights
#         - [`~loaders.FromSingleFileMixin.from_single_file`] for loading `.ckpt` files
#
#     Args:
#         vae ([`AutoencoderKL`]):
#             Variational Auto-Encoder (VAE) model to encode and decode images to and from latent representations.
#         text_encoder ([`~transformers.CLIPTextModel`]):
#             Frozen text-encoder ([clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14)).
#         tokenizer ([`~transformers.CLIPTokenizer`]):
#             A `CLIPTokenizer` to tokenize text.
#         unet ([`UNet2DConditionModel`]):
#             A `UNet2DConditionModel` to denoise the encoded image latents.
#         scheduler ([`SchedulerMixin`]):
#             A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
#             [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
#         safety_checker ([`StableDiffusionSafetyChecker`]):
#             Classification module that estimates whether generated images could be considered offensive or harmful.
#             Please refer to the [model card](https://huggingface.co/runwayml/stable-diffusion-v1-5) for more details
#             about a model's potential harms.
#         feature_extractor ([`~transformers.CLIPImageProcessor`]):
#             A `CLIPImageProcessor` to extract features from generated images; used as inputs to the `safety_checker`.
#     """
#     _optional_components = ["safety_checker", "feature_extractor"]
#
#     def __init__(
#         self,
#         vae: AutoencoderKL,
#         text_encoder: CLIPTextModel,
#         tokenizer: CLIPTokenizer,
#         unet: UNet2DConditionModel,
#         scheduler: KarrasDiffusionSchedulers,
#         safety_checker: StableDiffusionSafetyChecker,
#         feature_extractor: CLIPImageProcessor,
#         requires_safety_checker: bool = True,
#     ):
#         super().__init__()
#
#         if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
#             deprecation_message = (
#                 f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
#                 f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
#                 "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
#                 " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
#                 " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
#                 " file"
#             )
#             deprecate("steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False)
#             new_config = dict(scheduler.config)
#             new_config["steps_offset"] = 1
#             scheduler._internal_dict = FrozenDict(new_config)
#
#         if hasattr(scheduler.config, "clip_sample") and scheduler.config.clip_sample is True:
#             deprecation_message = (
#                 f"The configuration file of this scheduler: {scheduler} has not set the configuration `clip_sample`."
#                 " `clip_sample` should be set to False in the configuration file. Please make sure to update the"
#                 " config accordingly as not setting `clip_sample` in the config might lead to incorrect results in"
#                 " future versions. If you have downloaded this checkpoint from the Hugging Face Hub, it would be very"
#                 " nice if you could open a Pull request for the `scheduler/scheduler_config.json` file"
#             )
#             deprecate("clip_sample not set", "1.0.0", deprecation_message, standard_warn=False)
#             new_config = dict(scheduler.config)
#             new_config["clip_sample"] = False
#             scheduler._internal_dict = FrozenDict(new_config)
#
#         if safety_checker is None and requires_safety_checker:
#             logger.warning(
#                 f"You have disabled the safety checker for {self.__class__} by passing `safety_checker=None`. Ensure"
#                 " that you abide to the conditions of the Stable Diffusion license and do not expose unfiltered"
#                 " results in services or applications open to the public. Both the diffusers team and Hugging Face"
#                 " strongly recommend to keep the safety filter enabled in all public facing circumstances, disabling"
#                 " it only for use-cases that involve analyzing network behavior or auditing its results. For more"
#                 " information, please have a look at https://github.com/huggingface/diffusers/pull/254 ."
#             )
#
#         if safety_checker is not None and feature_extractor is None:
#             raise ValueError(
#                 "Make sure to define a feature extractor when loading {self.__class__} if you want to use the safety"
#                 " checker. If you do not want to use the safety checker, you can pass `'safety_checker=None'` instead."
#             )
#
#         is_unet_version_less_0_9_0 = hasattr(unet.config, "_diffusers_version") and version.parse(
#             version.parse(unet.config._diffusers_version).base_version
#         ) < version.parse("0.9.0.dev0")
#         is_unet_sample_size_less_64 = hasattr(unet.config, "sample_size") and unet.config.sample_size < 64
#         if is_unet_version_less_0_9_0 and is_unet_sample_size_less_64:
#             deprecation_message = (
#                 "The configuration file of the unet has set the default `sample_size` to smaller than"
#                 " 64 which seems highly unlikely. If your checkpoint is a fine-tuned version of any of the"
#                 " following: \n- CompVis/stable-diffusion-v1-4 \n- CompVis/stable-diffusion-v1-3 \n-"
#                 " CompVis/stable-diffusion-v1-2 \n- CompVis/stable-diffusion-v1-1 \n- runwayml/stable-diffusion-v1-5"
#                 " \n- runwayml/stable-diffusion-inpainting \n you should change 'sample_size' to 64 in the"
#                 " configuration file. Please make sure to update the config accordingly as leaving `sample_size=32`"
#                 " in the config might lead to incorrect results in future versions. If you have downloaded this"
#                 " checkpoint from the Hugging Face Hub, it would be very nice if you could open a Pull request for"
#                 " the `unet/config.json` file"
#             )
#             deprecate("sample_size<64", "1.0.0", deprecation_message, standard_warn=False)
#             new_config = dict(unet.config)
#             new_config["sample_size"] = 64
#             unet._internal_dict = FrozenDict(new_config)
#
#         self.register_modules(
#             vae=vae,
#             text_encoder=text_encoder,
#             tokenizer=tokenizer,
#             unet=unet,
#             scheduler=scheduler,
#             safety_checker=safety_checker,
#             feature_extractor=feature_extractor,
#         )
#         self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
#         self.image_processor = VaeImageProcessor(vae_scale_factor=self.vae_scale_factor)
#         self.register_to_config(requires_safety_checker=requires_safety_checker)
#
#     def enable_vae_slicing(self):
#         r"""
#         Enable sliced VAE decoding. When this option is enabled, the VAE will split the input tensor in slices to
#         compute decoding in several steps. This is useful to save some memory and allow larger batch sizes.
#         """
#         self.vae.enable_slicing()
#
#     def disable_vae_slicing(self):
#         r"""
#         Disable sliced VAE decoding. If `enable_vae_slicing` was previously enabled, this method will go back to
#         computing decoding in one step.
#         """
#         self.vae.disable_slicing()
#
#     def enable_vae_tiling(self):
#         r"""
#         Enable tiled VAE decoding. When this option is enabled, the VAE will split the input tensor into tiles to
#         compute decoding and encoding in several steps. This is useful for saving a large amount of memory and to allow
#         processing larger images.
#         """
#         self.vae.enable_tiling()
#
#     def disable_vae_tiling(self):
#         r"""
#         Disable tiled VAE decoding. If `enable_vae_tiling` was previously enabled, this method will go back to
#         computing decoding in one step.
#         """
#         self.vae.disable_tiling()
#
#     def enable_model_cpu_offload(self, gpu_id=0):
#         r"""
#         Offload all models to CPU to reduce memory usage with a low impact on performance. Moves one whole model at a
#         time to the GPU when its `forward` method is called, and the model remains in GPU until the next model runs.
#         Memory savings are lower than using `enable_sequential_cpu_offload`, but performance is much better due to the
#         iterative execution of the `unet`.
#         """
#         if is_accelerate_available() and is_accelerate_version(">=", "0.17.0.dev0"):
#             from accelerate import cpu_offload_with_hook
#         else:
#             raise ImportError("`enable_model_cpu_offload` requires `accelerate v0.17.0` or higher.")
#
#         device = torch.device(f"cuda:{gpu_id}")
#
#         if self.device.type != "cpu":
#             self.to("cpu", silence_dtype_warnings=True)
#             torch.cuda.empty_cache()  # otherwise we don't see the memory savings (but they probably exist)
#
#         hook = None
#         for cpu_offloaded_model in [self.text_encoder, self.unet, self.vae]:
#             _, hook = cpu_offload_with_hook(cpu_offloaded_model, device, prev_module_hook=hook)
#
#         if self.safety_checker is not None:
#             _, hook = cpu_offload_with_hook(self.safety_checker, device, prev_module_hook=hook)
#
#         # We'll offload the last model manually.
#         self.final_offload_hook = hook
#
#     def _encode_prompt(
#         self,
#         prompt,
#         device,
#         num_images_per_prompt,
#         do_classifier_free_guidance,
#         negative_prompt=None,
#         prompt_embeds: Optional[torch.FloatTensor] = None,
#         negative_prompt_embeds: Optional[torch.FloatTensor] = None,
#         lora_scale: Optional[float] = None,
#     ):
#         r"""
#         Encodes the prompt into text encoder hidden states.
#
#         Args:
#              prompt (`str` or `List[str]`, *optional*):
#                 prompt to be encoded
#             device: (`torch.device`):
#                 torch device
#             num_images_per_prompt (`int`):
#                 number of images that should be generated per prompt
#             do_classifier_free_guidance (`bool`):
#                 whether to use classifier free guidance or not
#             negative_prompt (`str` or `List[str]`, *optional*):
#                 The prompt or prompts not to guide the image generation. If not defined, one has to pass
#                 `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
#                 less than `1`).
#             prompt_embeds (`torch.FloatTensor`, *optional*):
#                 Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
#                 provided, text embeddings will be generated from `prompt` input argument.
#             negative_prompt_embeds (`torch.FloatTensor`, *optional*):
#                 Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
#                 weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
#                 argument.
#             lora_scale (`float`, *optional*):
#                 A lora scale that will be applied to all LoRA layers of the text encoder if LoRA layers are loaded.
#         """
#         # set lora scale so that monkey patched LoRA
#         # function of text encoder can correctly access it
#         if lora_scale is not None and isinstance(self, LoraLoaderMixin):
#             self._lora_scale = lora_scale
#
#         if prompt is not None and isinstance(prompt, str):
#             batch_size = 1
#         elif prompt is not None and isinstance(prompt, list):
#             batch_size = len(prompt)
#         else:
#             batch_size = prompt_embeds.shape[0]
#
#         if prompt_embeds is None:
#             # textual inversion: procecss multi-vector tokens if necessary
#             if isinstance(self, TextualInversionLoaderMixin):
#                 prompt = self.maybe_convert_prompt(prompt, self.tokenizer)
#
#             text_inputs = self.tokenizer(
#                 prompt,
#                 padding="max_length",
#                 max_length=self.tokenizer.model_max_length,
#                 truncation=True,
#                 return_tensors="pt",
#             )
#             text_input_ids = text_inputs.input_ids
#             untruncated_ids = self.tokenizer(prompt, padding="longest", return_tensors="pt").input_ids
#
#             if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(
#                 text_input_ids, untruncated_ids
#             ):
#                 removed_text = self.tokenizer.batch_decode(
#                     untruncated_ids[:, self.tokenizer.model_max_length - 1 : -1]
#                 )
#                 logger.warning(
#                     "The following part of your input was truncated because CLIP can only handle sequences up to"
#                     f" {self.tokenizer.model_max_length} tokens: {removed_text}"
#                 )
#
#             if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
#                 attention_mask = text_inputs.attention_mask.to(device)
#             else:
#                 attention_mask = None
#
#             prompt_embeds = self.text_encoder(
#                 text_input_ids.to(device),
#                 attention_mask=attention_mask,
#             )
#             prompt_embeds = prompt_embeds[0]
#
#         if self.text_encoder is not None:
#             prompt_embeds_dtype = self.text_encoder.dtype
#         elif self.unet is not None:
#             prompt_embeds_dtype = self.unet.dtype
#         else:
#             prompt_embeds_dtype = prompt_embeds.dtype
#
#         prompt_embeds = prompt_embeds.to(dtype=prompt_embeds_dtype, device=device)
#
#         bs_embed, seq_len, _ = prompt_embeds.shape
#         # duplicate text embeddings for each generation per prompt, using mps friendly method
#         prompt_embeds = prompt_embeds.repeat(1, num_images_per_prompt, 1)
#         prompt_embeds = prompt_embeds.view(bs_embed * num_images_per_prompt, seq_len, -1)
#
#         # get unconditional embeddings for classifier free guidance
#         if do_classifier_free_guidance and negative_prompt_embeds is None:
#             uncond_tokens: List[str]
#             if negative_prompt is None:
#                 uncond_tokens = [""] * batch_size
#             elif prompt is not None and type(prompt) is not type(negative_prompt):
#                 raise TypeError(
#                     f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
#                     f" {type(prompt)}."
#                 )
#             elif isinstance(negative_prompt, str):
#                 uncond_tokens = [negative_prompt]
#             elif batch_size != len(negative_prompt):
#                 raise ValueError(
#                     f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
#                     f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
#                     " the batch size of `prompt`."
#                 )
#             else:
#                 uncond_tokens = negative_prompt
#
#             # textual inversion: procecss multi-vector tokens if necessary
#             if isinstance(self, TextualInversionLoaderMixin):
#                 uncond_tokens = self.maybe_convert_prompt(uncond_tokens, self.tokenizer)
#
#             max_length = prompt_embeds.shape[1]
#             uncond_input = self.tokenizer(
#                 uncond_tokens,
#                 padding="max_length",
#                 max_length=max_length,
#                 truncation=True,
#                 return_tensors="pt",
#             )
#
#             if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
#                 attention_mask = uncond_input.attention_mask.to(device)
#             else:
#                 attention_mask = None
#
#             negative_prompt_embeds = self.text_encoder(
#                 uncond_input.input_ids.to(device),
#                 attention_mask=attention_mask,
#             )
#             negative_prompt_embeds = negative_prompt_embeds[0]
#
#         if do_classifier_free_guidance:
#             # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
#             seq_len = negative_prompt_embeds.shape[1]
#
#             negative_prompt_embeds = negative_prompt_embeds.to(dtype=prompt_embeds_dtype, device=device)
#
#             negative_prompt_embeds = negative_prompt_embeds.repeat(1, num_images_per_prompt, 1)
#             negative_prompt_embeds = negative_prompt_embeds.view(batch_size * num_images_per_prompt, seq_len, -1)
#
#             # For classifier free guidance, we need to do two forward passes.
#             # Here we concatenate the unconditional and text embeddings into a single batch
#             # to avoid doing two forward passes
#             prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds])
#
#         return prompt_embeds
#
#     def run_safety_checker(self, image, device, dtype):
#         if self.safety_checker is None:
#             has_nsfw_concept = None
#         else:
#             if torch.is_tensor(image):
#                 feature_extractor_input = self.image_processor.postprocess(image, output_type="pil")
#             else:
#                 feature_extractor_input = self.image_processor.numpy_to_pil(image)
#             safety_checker_input = self.feature_extractor(feature_extractor_input, return_tensors="pt").to(device)
#             image, has_nsfw_concept = self.safety_checker(
#                 images=image, clip_input=safety_checker_input.pixel_values.to(dtype)
#             )
#         return image, has_nsfw_concept
#
#     def decode_latents(self, latents):
#         warnings.warn(
#             "The decode_latents method is deprecated and will be removed in a future version. Please"
#             " use VaeImageProcessor instead",
#             FutureWarning,
#         )
#         latents = 1 / self.vae.config.scaling_factor * latents
#         image = self.vae.decode(latents, return_dict=False)[0]
#         image = (image / 2 + 0.5).clamp(0, 1)
#         # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
#         image = image.cpu().permute(0, 2, 3, 1).float().numpy()
#         return image
#
#     def prepare_extra_step_kwargs(self, generator, eta):
#         # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
#         # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
#         # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
#         # and should be between [0, 1]
#
#         accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
#         extra_step_kwargs = {}
#         if accepts_eta:
#             extra_step_kwargs["eta"] = eta
#
#         # check if the scheduler accepts generator
#         accepts_generator = "generator" in set(inspect.signature(self.scheduler.step).parameters.keys())
#         if accepts_generator:
#             extra_step_kwargs["generator"] = generator
#         return extra_step_kwargs
#
#     def check_inputs(
#         self,
#         prompt,
#         height,
#         width,
#         callback_steps,
#         negative_prompt=None,
#         prompt_embeds=None,
#         negative_prompt_embeds=None,
#     ):
#         if height % 8 != 0 or width % 8 != 0:
#             raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")
#
#         if (callback_steps is None) or (
#             callback_steps is not None and (not isinstance(callback_steps, int) or callback_steps <= 0)
#         ):
#             raise ValueError(
#                 f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
#                 f" {type(callback_steps)}."
#             )
#
#         if prompt is not None and prompt_embeds is not None:
#             raise ValueError(
#                 f"Cannot forward both `prompt`: {prompt} and `prompt_embeds`: {prompt_embeds}. Please make sure to"
#                 " only forward one of the two."
#             )
#         elif prompt is None and prompt_embeds is None:
#             raise ValueError(
#                 "Provide either `prompt` or `prompt_embeds`. Cannot leave both `prompt` and `prompt_embeds` undefined."
#             )
#         elif prompt is not None and (not isinstance(prompt, str) and not isinstance(prompt, list)):
#             raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")
#
#         if negative_prompt is not None and negative_prompt_embeds is not None:
#             raise ValueError(
#                 f"Cannot forward both `negative_prompt`: {negative_prompt} and `negative_prompt_embeds`:"
#                 f" {negative_prompt_embeds}. Please make sure to only forward one of the two."
#             )
#
#         if prompt_embeds is not None and negative_prompt_embeds is not None:
#             if prompt_embeds.shape != negative_prompt_embeds.shape:
#                 raise ValueError(
#                     "`prompt_embeds` and `negative_prompt_embeds` must have the same shape when passed directly, but"
#                     f" got: `prompt_embeds` {prompt_embeds.shape} != `negative_prompt_embeds`"
#                     f" {negative_prompt_embeds.shape}."
#                 )
#
#     def prepare_latents(self, batch_size, num_channels_latents, height, width, dtype, device, generator, latents=None):
#         shape = (batch_size, num_channels_latents, height // self.vae_scale_factor, width // self.vae_scale_factor)
#         if isinstance(generator, list) and len(generator) != batch_size:
#             raise ValueError(
#                 f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
#                 f" size of {batch_size}. Make sure the batch size matches the length of the generators."
#             )
#
#         if latents is None:
#             latents = randn_tensor(shape, generator=generator, device=device, dtype=dtype)
#         else:
#             latents = latents.to(device)
#
#         # scale the initial noise by the standard deviation required by the scheduler
#         latents = latents * self.scheduler.init_noise_sigma
#         return latents
#
#     @torch.no_grad()
#     @replace_example_docstring(EXAMPLE_DOC_STRING)
#     def __call__(
#         self,
#         prompt: Union[str, List[str]] = None,
#         height: Optional[int] = None,
#         width: Optional[int] = None,
#         num_inference_steps: int = 50,
#         guidance_scale: float = 7.5,
#         negative_prompt: Optional[Union[str, List[str]]] = None,
#         num_images_per_prompt: Optional[int] = 1,
#         eta: float = 0.0,
#         generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
#         latents: Optional[torch.FloatTensor] = None,
#         prompt_embeds: Optional[torch.FloatTensor] = None,
#         negative_prompt_embeds: Optional[torch.FloatTensor] = None,
#         output_type: Optional[str] = "pil",
#         return_dict: bool = True,
#         callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
#         callback_steps: int = 1,
#         cross_attention_kwargs: Optional[Dict[str, Any]] = None,
#         guidance_rescale: float = 0.0,
#     ):
#         r"""
#         The call function to the pipeline for generation.
#
#         Args:
#             prompt (`str` or `List[str]`, *optional*):
#                 The prompt or prompts to guide image generation. If not defined, you need to pass `prompt_embeds`.
#             height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
#                 The height in pixels of the generated image.
#             width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
#                 The width in pixels of the generated image.
#             num_inference_steps (`int`, *optional*, defaults to 50):
#                 The number of denoising steps. More denoising steps usually lead to a higher quality image at the
#                 expense of slower inference.
#             guidance_scale (`float`, *optional*, defaults to 7.5):
#                 A higher guidance scale value encourages the model to generate images closely linked to the text
#                 `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
#             negative_prompt (`str` or `List[str]`, *optional*):
#                 The prompt or prompts to guide what to not include in image generation. If not defined, you need to
#                 pass `negative_prompt_embeds` instead. Ignored when not using guidance (`guidance_scale < 1`).
#             num_images_per_prompt (`int`, *optional*, defaults to 1):
#                 The number of images to generate per prompt.
#             eta (`float`, *optional*, defaults to 0.0):
#                 Corresponds to parameter eta (η) from the [DDIM](https://arxiv.org/abs/2010.02502) paper. Only applies
#                 to the [`~schedulers.DDIMScheduler`], and is ignored in other schedulers.
#             generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
#                 A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
#                 generation deterministic.
#             latents (`torch.FloatTensor`, *optional*):
#                 Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for image
#                 generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
#                 tensor is generated by sampling using the supplied random `generator`.
#             prompt_embeds (`torch.FloatTensor`, *optional*):
#                 Pre-generated text embeddings. Can be used to easily tweak text inputs (prompt weighting). If not
#                 provided, text embeddings are generated from the `prompt` input argument.
#             negative_prompt_embeds (`torch.FloatTensor`, *optional*):
#                 Pre-generated negative text embeddings. Can be used to easily tweak text inputs (prompt weighting). If
#                 not provided, `negative_prompt_embeds` are generated from the `negative_prompt` input argument.
#             output_type (`str`, *optional*, defaults to `"pil"`):
#                 The output format of the generated image. Choose between `PIL.Image` or `np.array`.
#             return_dict (`bool`, *optional*, defaults to `True`):
#                 Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
#                 plain tuple.
#             callback (`Callable`, *optional*):
#                 A function that calls every `callback_steps` steps during inference. The function is called with the
#                 following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
#             callback_steps (`int`, *optional*, defaults to 1):
#                 The frequency at which the `callback` function is called. If not specified, the callback is called at
#                 every step.
#             cross_attention_kwargs (`dict`, *optional*):
#                 A kwargs dictionary that if specified is passed along to the [`AttentionProcessor`] as defined in
#                 [`self.processor`](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).
#             guidance_rescale (`float`, *optional*, defaults to 0.7):
#                 Guidance rescale factor from [Common Diffusion Noise Schedules and Sample Steps are
#                 Flawed](https://arxiv.org/pdf/2305.08891.pdf). Guidance rescale factor should fix overexposure when
#                 using zero terminal SNR.
#
#         Examples:
#
#         Returns:
#             [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
#                 If `return_dict` is `True`, [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] is returned,
#                 otherwise a `tuple` is returned where the first element is a list with the generated images and the
#                 second element is a list of `bool`s indicating whether the corresponding generated image contains
#                 "not-safe-for-work" (nsfw) content.
#         """
#         # 0. Default height and width to unet
#         height = height or self.unet.config.sample_size * self.vae_scale_factor
#         width = width or self.unet.config.sample_size * self.vae_scale_factor
#
#         # 1. Check inputs. Raise error if not correct
#         self.check_inputs(
#             prompt, height, width, callback_steps, negative_prompt, prompt_embeds, negative_prompt_embeds
#         )
#
#         # 2. Define call parameters
#         if prompt is not None and isinstance(prompt, str):
#             batch_size = 1
#         elif prompt is not None and isinstance(prompt, list):
#             batch_size = len(prompt)
#         else:
#             batch_size = prompt_embeds.shape[0]
#
#         device = self._execution_device
#         # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
#         # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
#         # corresponds to doing no classifier free guidance.
#         do_classifier_free_guidance = guidance_scale > 1.0
#
#         # 3. Encode input prompt
#         text_encoder_lora_scale = (
#             cross_attention_kwargs.get("scale", None) if cross_attention_kwargs is not None else None
#         )
#         prompt_embeds = self._encode_prompt(
#             prompt,
#             device,
#             num_images_per_prompt,
#             do_classifier_free_guidance,
#             negative_prompt,
#             prompt_embeds=prompt_embeds,
#             negative_prompt_embeds=negative_prompt_embeds,
#             lora_scale=text_encoder_lora_scale,
#         )
#
#         # 4. Prepare timesteps
#         self.scheduler.set_timesteps(num_inference_steps, device=device)
#         timesteps = self.scheduler.timesteps
#
#         # 5. Prepare latent variables
#         num_channels_latents = self.unet.config.in_channels
#         latents = self.prepare_latents(
#             batch_size * num_images_per_prompt,
#             num_channels_latents,
#             height,
#             width,
#             prompt_embeds.dtype,
#             device,
#             generator,
#             latents,
#         )
#
#         # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
#         extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
#
#         # 7. Denoising loop
#         num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
#         with self.progress_bar(total=num_inference_steps) as progress_bar:
#             for i, t in enumerate(timesteps):
#                 # expand the latents if we are doing classifier free guidance
#                 latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
#                 latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
#
#                 # predict the noise residual
#                 noise_pred = self.unet(
#                     latent_model_input,
#                     t,
#                     encoder_hidden_states=prompt_embeds,
#                     cross_attention_kwargs=cross_attention_kwargs,
#                     return_dict=False,
#                 )[0]
#
#                 # perform guidance
#                 if do_classifier_free_guidance:
#                     noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
#                     noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
#
#                 if do_classifier_free_guidance and guidance_rescale > 0.0:
#                     # Based on 3.4. in https://arxiv.org/pdf/2305.08891.pdf
#                     noise_pred = rescale_noise_cfg(noise_pred, noise_pred_text, guidance_rescale=guidance_rescale)
#
#                 # compute the previous noisy sample x_t -> x_t-1
#                 latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs, return_dict=False)[0]
#
#                 # call the callback, if provided
#                 if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
#                     progress_bar.update()
#                     if callback is not None and i % callback_steps == 0:
#                         callback(i, t, latents)
#
#         if not output_type == "latent":
#             image = self.vae.decode(latents / self.vae.config.scaling_factor, return_dict=False)[0]
#             image, has_nsfw_concept = self.run_safety_checker(image, device, prompt_embeds.dtype)
#         else:
#             image = latents
#             has_nsfw_concept = None
#
#         if has_nsfw_concept is None:
#             do_denormalize = [True] * image.shape[0]
#         else:
#             do_denormalize = [not has_nsfw for has_nsfw in has_nsfw_concept]
#
#         image = self.image_processor.postprocess(image, output_type=output_type, do_denormalize=do_denormalize)
#
#         # Offload last model to CPU
#         if hasattr(self, "final_offload_hook") and self.final_offload_hook is not None:
#             self.final_offload_hook.offload()
#
#         if not return_dict:
#             return (image, has_nsfw_concept)
#
#         return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)
