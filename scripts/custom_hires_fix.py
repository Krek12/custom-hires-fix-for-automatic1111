import math
from os.path import exists

from tqdm import trange
from modules import scripts, shared, processing,  sd_schedulers, sd_samplers, script_callbacks, rng
from modules import images, devices, prompt_parser, sd_models, ui_components, sd_models, extra_networks
import modules.images as images
import k_diffusion

import gradio as gr
import numpy as np
from PIL import Image, ImageEnhance
import torch
import importlib
from copy import copy
from PIL import Image
import json
import gradio as gr

quote_swap = str.maketrans('\'"', '"\'')

def safe_import(import_name, pkg_name = None):
    try:
        __import__(import_name)
    except Exception:
        pkg_name = pkg_name or import_name
        import pip
        if hasattr(pip, 'main'):
            pip.main(['install', pkg_name])
        else:
            pip._internal.main(['install', pkg_name])
        __import__(import_name)


safe_import('kornia')
safe_import('omegaconf')
safe_import('pathlib')
from omegaconf import DictConfig, OmegaConf
from pathlib import Path
import kornia
from skimage import exposure

config_path = Path(__file__).parent.resolve() / '../config.yaml'


class CustomHiresFix(scripts.Script):
    def __init__(self):
        super().__init__()
        if not exists(config_path):
            open(config_path, 'w').close()
        self.config: DictConfig = OmegaConf.load(config_path)
        self.callback_set = False
        self.orig_clip_skip = None
        self.cfg = 0
        self.p: processing.StableDiffusionProcessing = None
        self.pp = None
        self.sampler = None
        self.cond = None
        self.uncond = None
        self.prompt = ""
        self.negative_prompt = ""
        self.step = None
        self.tv = None
        self.width = None
        self.height = None
        self.extra_data = None
        self.use_cn = False
        self.external_code = None
        self.cn_image = None
        self.cn_units = []

    def title(self):
        return "Custom Hires Fix"

    def show(self, is_img2img):
        return scripts.AlwaysVisible
    
    def ui(self, is_img2img):
        sampler_names = ['Restart + DPM++ 3M SDE'] + [x.name for x in sd_samplers.visible_samplers()]
        scheduler_names = ['Use same scheduler'] + [x.label for x in sd_schedulers.schedulers]
        
        with gr.Accordion(label='Custom hires fix', open=False):
            enable = gr.Checkbox(label='Enable extension', value=self.config.get('enable', False))
            with gr.Row():
                width = gr.Slider(minimum=512, maximum=2048, step=8,
                                  label="Upscale width to",
                                  value=self.config.get('width', 1024), allow_flagging='never', show_progress=False)
                height = gr.Slider(minimum=512, maximum=2048, step=8,
                                   label="Upscale height to",
                                   value=self.config.get('height', 0), allow_flagging='never', show_progress=False)
                steps = gr.Slider(minimum=8, maximum=25, step=1,
                                  label="Steps",
                                  value=self.config.get('steps', 15))
                
            with gr.Row():
                prompt = gr.Textbox(label='Prompt for upscale (added to generation prompt)',
                                    placeholder='Leave empty for using generation prompt',
                                    value=self.prompt)
            with gr.Row():
                negative_prompt = gr.Textbox(label='Negative prompt for upscale (replaces generation prompt)',
                                             placeholder='Leave empty for using generation negative prompt',
                                             value=self.negative_prompt)
            with gr.Row():
                first_upscaler = gr.Dropdown([*[x.name for x in shared.sd_upscalers
                                                if x.name not in ['None', 'Nearest', 'LDSR']]],
                                             label='First upscaler',
                                             value=self.config.get('first_upscaler', 'R-ESRGAN 4x+'))
                second_upscaler = gr.Dropdown([*[x.name for x in shared.sd_upscalers
                                                 if x.name not in ['None', 'Nearest', 'LDSR']]],
                                              label='Second upscaler',
                                              value=self.config.get('second_upscaler', 'R-ESRGAN 4x+'))
            with gr.Row():
                first_latent = gr.Slider(minimum=0.0, maximum=1.0, step=0.01, 
                                         label="Latent upscale ratio (1)",
                                         value=self.config.get('first_latent', 0.3))
                second_latent = gr.Slider(minimum=0.0, maximum=1.0, step=0.01, 
                                         label="Latent upscale ratio (2)",
                                         value=self.config.get('second_latent', 0.1))
            with gr.Row():
                filter = gr.Dropdown(['Noise sync (sharp)', 'Morphological (smooth)', 'Combined (balanced)'],
                                     label='Filter mode',
                                     value=self.config.get('filter', 'Noise sync (sharp)'))
                strength = gr.Slider(minimum=1.0, maximum=3.5, step=0.1, label="Generation strength",
                                     value=self.config.get('strength', 2.0))
                denoise_offset = gr.Slider(minimum=-0.05, maximum=0.15, step=0.01,
                                           label="Denoise offset",
                                           value=self.config.get('denoise_offset', 0.05))
            with gr.Accordion(label='Extra', open=False):
                with gr.Row():
                    filter_offset = gr.Slider(minimum=-1.0, maximum=1.0, step=0.1,
                                              label="Filter offset (higher - smoother)",
                                              value=self.config.get('filter_offset', 0.0))
                    clip_skip = gr.Slider(minimum=0, maximum=5, step=1,
                                          label="Clip skip for upscale (0 - not change)",
                                          value=self.config.get('clip_skip', 0))
                with gr.Row():
                    start_control_at = gr.Slider(minimum=0.0, maximum=0.7, step=0.01,
                                                 label="CN start for enabled units",
                                                 value=self.config.get('start_control_at', 0.0))
                    cn_ref = gr.Checkbox(label='Use last image for reference', value=self.config.get('cn_ref', False))
                with gr.Row():
                    sampler = gr.Dropdown(sampler_names, label='Sampler', value=sampler_names[0])
                    scheduler = gr.Dropdown(label='Schedule type', elem_id=f"{self.tabname}_scheduler", choices=scheduler_names, value=scheduler_names[0])
                    cfg = gr.Slider(minimum=0, maximum=30, step=0.5, label="CFG Scale", value=self.cfg)
                    
                    
        if is_img2img:
            width.change(fn=lambda x: gr.update(value=0), inputs=width, outputs=height)
            height.change(fn=lambda x: gr.update(value=0), inputs=height, outputs=width)
        else:
            width.change(fn=lambda x: gr.update(value=0), inputs=width, outputs=height)
            height.change(fn=lambda x: gr.update(value=0), inputs=height, outputs=width)

        ui = [enable, width, height, steps, first_upscaler, second_upscaler, first_latent, second_latent, prompt, 
              negative_prompt, strength, filter, filter_offset, denoise_offset, clip_skip, sampler, cfg, scheduler, cn_ref, start_control_at]
        for elem in ui:
            setattr(elem, "do_not_save_to_config", True)
        return ui

    def process(self, p, *args, **kwargs):
        self.p = p
        self.cn_units = []
        try:
            self.external_code = importlib.import_module('extensions.sd-webui-controlnet.scripts.external_code', 'external_code')
            cn_units = self.external_code.get_all_units_in_processing(p)
            for unit in cn_units:
                self.cn_units += [unit]
            self.use_cn = len(self.cn_units) > 0
        except ImportError:
            self.use_cn = False
        
    def postprocess_image(self, p, pp: scripts.PostprocessImageArgs,
                          enable, width, height, steps, first_upscaler, second_upscaler, first_latent, second_latent, prompt,
                          negative_prompt, strength, filter, filter_offset, denoise_offset, clip_skip, sampler, cfg, scheduler, cn_ref, start_control_at
                          ):
        if not enable:
            return
        self.step = 0
        self.pp = pp
        self.config.width = width
        self.config.height = height
        self.config.prompt = prompt.strip()
        self.config.negative_prompt = negative_prompt.strip()
        self.config.steps = steps
        self.config.first_upscaler = first_upscaler
        self.config.second_upscaler = second_upscaler
        self.config.first_latent = first_latent
        self.config.second_latent = second_latent
        self.config.strength = strength
        self.config.filter = filter
        self.config.filter_offset = filter_offset
        self.config.denoise_offset = denoise_offset
        self.config.clip_skip = clip_skip
        self.config.sampler = sampler
        self.config.cn_ref = cn_ref
        self.config.start_control_at = start_control_at
        self.orig_clip_skip = shared.opts.CLIP_stop_at_last_layers
        self.cfg = cfg if cfg else p.cfg_scale
        
        if clip_skip > 0:
            shared.opts.CLIP_stop_at_last_layers = clip_skip
        if 'Restart' in self.config.sampler:
            self.sampler = sd_samplers.create_sampler('Restart', p.sd_model)
        else:
            self.sampler = sd_samplers.create_sampler(sampler, p.sd_model)
            
        def denoise_callback(params: script_callbacks.CFGDenoiserParams):
            if params.sampling_step > 0:
                p.cfg_scale = self.cfg
            if self.step == 1 and self.config.strength != 1.0:
                params.sigma[-1] = params.sigma[0] * (1 - (1 - self.config.strength) / 100)
            elif self.step == 2 and self.config.filter == 'Noise sync (sharp)':
                params.sigma[-1] = params.sigma[0] * (1 - (self.tv - 1 + self.config.filter_offset - (self.config.denoise_offset * 5)) / 50)
            elif self.step == 2 and self.config.filter == 'Combined (balanced)':
                params.sigma[-1] = params.sigma[0] * (1 - (self.tv - 1 + self.config.filter_offset - (self.config.denoise_offset * 5)) / 100)

        if self.callback_set is False:
            script_callbacks.on_cfg_denoiser(denoise_callback)
            self.callback_set = True

        _, loras_act = extra_networks.parse_prompt(prompt)
        extra_networks.activate(p, loras_act)
        _, loras_deact = extra_networks.parse_prompt(negative_prompt)
        extra_networks.deactivate(p, loras_deact)
        
        self.cn_image = pp.image
        
        with devices.autocast():
            shared.state.nextjob()
            x = self.gen(pp.image)
            shared.state.nextjob()
            x = self.filter(x)
        shared.opts.CLIP_stop_at_last_layers = self.orig_clip_skip
        sd_models.apply_token_merging(p.sd_model, p.get_token_merging_ratio())
        pp.image = x
        extra_networks.deactivate(p, loras_act)
        OmegaConf.save(self.config, config_path)
        
    def enable_cn(self, image: np.ndarray):
        for unit in self.cn_units:
            if unit.model != 'None':
                unit.guidance_start = self.config.start_control_at if unit.enabled else unit.guidance_start
                unit.processor_res = min(image.shape[0], image.shape[0])
                unit.enabled = True
                if unit.image is None:
                    unit.image = image
                self.p.width = image.shape[1]
                self.p.height = image.shape[0]
        self.external_code.update_cn_script_in_processing(self.p, self.cn_units)
        for script in self.p.scripts.alwayson_scripts:
            if script.title().lower() == 'controlnet':
                script.controlnet_hack(self.p)

    def process_prompt(self):
        prompt = self.p.prompt.strip().split('AND', 1)[0]
        if self.config.prompt != '':
            prompt = f'{prompt} {self.config.prompt}'

        if self.config.negative_prompt != '':
            negative_prompt = self.config.negative_prompt
        else:
            negative_prompt = self.p.negative_prompt.strip()

        with devices.autocast():
            if self.width is not None and self.height is not None and hasattr(prompt_parser, 'SdConditioning'):
                c = prompt_parser.SdConditioning([prompt], False, self.width, self.height)
                uc = prompt_parser.SdConditioning([negative_prompt], False, self.width, self.height)
            else:
                c = [prompt]
                uc = [negative_prompt]
            self.cond = prompt_parser.get_multicond_learned_conditioning(shared.sd_model, c, self.config.steps)
            self.uncond = prompt_parser.get_learned_conditioning(shared.sd_model, uc, self.config.steps)

    def gen(self, x):
        self.step = 1
        ratio = x.width / x.height
        self.width = self.config.width if self.config.width > 0 else int(self.config.height * ratio)
        self.height = self.config.height if self.config.height > 0 else int(self.config.width / ratio)
        self.width = int((self.width - x.width) // 2 + x.width)
        self.height = int((self.height - x.height) // 2 + x.height)
        sd_models.apply_token_merging(self.p.sd_model, self.p.get_token_merging_ratio(for_hr=True) / 2)
        
        if self.use_cn:
            self.enable_cn(np.array(self.cn_image.resize((self.width, self.height))))
            
        with devices.autocast(), torch.inference_mode():
            self.process_prompt()
        
        x_big = None
        if self.config.first_latent > 0:
            image = np.array(x).astype(np.float32) / 255.0
            image = np.moveaxis(image, 2, 0)
            decoded_sample = torch.from_numpy(image)
            decoded_sample = decoded_sample.to(shared.device).to(devices.dtype_vae)
            decoded_sample = 2.0 * decoded_sample - 1.0
            encoded_sample = shared.sd_model.encode_first_stage(decoded_sample.unsqueeze(0).to(devices.dtype_vae))
            sample = shared.sd_model.get_first_stage_encoding(encoded_sample)
            x_big = torch.nn.functional.interpolate(sample, (self.height // 8, self.width // 8), mode='nearest')
            
        if self.config.first_latent < 1:
            x = images.resize_image(0, x, self.width, self.height,
                                    upscaler_name=self.config.first_upscaler)
            image = np.array(x).astype(np.float32) / 255.0
            image = np.moveaxis(image, 2, 0)
            decoded_sample = torch.from_numpy(image)
            decoded_sample = decoded_sample.to(shared.device).to(devices.dtype_vae)
            decoded_sample = 2.0 * decoded_sample - 1.0
            encoded_sample = shared.sd_model.encode_first_stage(decoded_sample.unsqueeze(0).to(devices.dtype_vae))
            sample = shared.sd_model.get_first_stage_encoding(encoded_sample)
        else:
            sample = x_big
        if x_big is not None and self.config.first_latent != 1:
            sample = (sample * (1 - self.config.first_latent)) + (x_big * self.config.first_latent)
        image_conditioning = self.p.img2img_image_conditioning(decoded_sample, sample)
        
        noise = torch.zeros_like(sample)
        noise = kornia.augmentation.RandomGaussianNoise(mean=0.0, std=1.0, p=1.0)(noise)
        steps = int(max(((self.p.steps - self.config.steps) / 2) + self.config.steps, self.config.steps))
        self.p.denoising_strength = 0.45 + self.config.denoise_offset * 0.2
        self.p.cfg_scale = self.cfg + 3

        def denoiser_override(n):
            sigmas = k_diffusion.sampling.get_sigmas_polyexponential(n, 0.01, 15, 0.5, devices.device)
            return sigmas
        
        self.p.rng = rng.ImageRNG(sample.shape[1:], self.p.seeds, subseeds=self.p.subseeds, 
                                  subseed_strength=self.p.subseed_strength, 
                                  seed_resize_from_h=self.p.seed_resize_from_h, seed_resize_from_w=self.p.seed_resize_from_w)
        
        self.p.sampler_noise_scheduler_override = denoiser_override
        self.p.batch_size = 1
        sample = self.sampler.sample_img2img(self.p, sample.to(devices.dtype), noise, self.cond, self.uncond,
                                             steps=steps, image_conditioning=image_conditioning).to(devices.dtype_vae)
        b, c, w, h = sample.size()
        self.tv = kornia.losses.TotalVariation()(sample).mean() / (w * h)
        devices.torch_gc()
        decoded_sample = processing.decode_first_stage(shared.sd_model, sample)
        if math.isnan(decoded_sample.min()):
            devices.torch_gc()
            sample = torch.clamp(sample, -3, 3)
            decoded_sample = processing.decode_first_stage(shared.sd_model, sample)
        decoded_sample = torch.clamp((decoded_sample + 1.0) / 2.0, min=0.0, max=1.0).squeeze()
        x_sample = 255. * np.moveaxis(decoded_sample.cpu().numpy(), 0, 2)
        x_sample = x_sample.astype(np.uint8)
        image = Image.fromarray(x_sample)
        return image
        
            
    def filter(self, x):
        if 'Restart' == self.config.sampler:
            self.sampler = sd_samplers.create_sampler('Restart', shared.sd_model)
        elif 'Restart + DPM++ 3M SDE' == self.config.sampler:
            self.sampler = sd_samplers.create_sampler('DPM++ 3M SDE', shared.sd_model)
        self.step = 2
        ratio = x.width / x.height
        self.width = self.config.width if self.config.width > 0 else int(self.config.height * ratio)
        self.height = self.config.height if self.config.height > 0 else int(self.config.width / ratio)
        sd_models.apply_token_merging(self.p.sd_model, self.p.get_token_merging_ratio(for_hr=True))
        
        if self.use_cn:
            self.cn_image = x if self.config.cn_ref else self.cn_image
            self.enable_cn(np.array(self.cn_image.resize((self.width, self.height))))
        
        with devices.autocast(), torch.inference_mode():
            self.process_prompt()
        
        x_big = None
        if self.config.second_latent > 0:
            image = np.array(x).astype(np.float32) / 255.0
            image = np.moveaxis(image, 2, 0)
            decoded_sample = torch.from_numpy(image)
            decoded_sample = decoded_sample.to(shared.device).to(devices.dtype_vae)
            decoded_sample = 2.0 * decoded_sample - 1.0
            encoded_sample = shared.sd_model.encode_first_stage(decoded_sample.unsqueeze(0).to(devices.dtype_vae))
            sample = shared.sd_model.get_first_stage_encoding(encoded_sample)
            x_big = torch.nn.functional.interpolate(sample, (self.height // 8, self.width // 8), mode='nearest')
            
        if self.config.second_latent < 1:
            x = images.resize_image(0, x, self.width, self.height, upscaler_name=self.config.second_upscaler)
            image = np.array(x).astype(np.float32) / 255.0
            image = np.moveaxis(image, 2, 0)
            decoded_sample = torch.from_numpy(image)
            decoded_sample = decoded_sample.to(shared.device).to(devices.dtype_vae)
            decoded_sample = 2.0 * decoded_sample - 1.0
            encoded_sample = shared.sd_model.encode_first_stage(decoded_sample.unsqueeze(0).to(devices.dtype_vae))
            sample = shared.sd_model.get_first_stage_encoding(encoded_sample)
        else:
            sample = x_big
        if x_big is not None and self.config.second_latent != 1:
            sample = (sample * (1 - self.config.second_latent)) + (x_big * self.config.second_latent)
        image_conditioning = self.p.img2img_image_conditioning(decoded_sample, sample)
        
        noise = torch.zeros_like(sample)
        noise = kornia.augmentation.RandomGaussianNoise(mean=0.0, std=1.0, p=1.0)(noise)
        self.p.denoising_strength = 0.45 + self.config.denoise_offset
        self.p.cfg_scale = self.cfg + 3

        if self.config.filter == 'Morphological (smooth)':
            noise_mask = kornia.morphology.gradient(sample, torch.ones(5, 5).to(devices.device))
            noise_mask = kornia.filters.median_blur(noise_mask, (3, 3))
            noise_mask = (0.1 + noise_mask / noise_mask.max()) * (max(
                (1.75 - (self.tv - 1) * 4), 1.75) - self.config.filter_offset)
            noise = noise * noise_mask
        elif self.config.filter == 'Combined (balanced)':
            noise_mask = kornia.morphology.gradient(sample, torch.ones(5, 5).to(devices.device))
            noise_mask = kornia.filters.median_blur(noise_mask, (3, 3))
            noise_mask = (0.1 + noise_mask / noise_mask.max()) * (max(
                (1.75 - (self.tv - 1) / 2), 1.75) - self.config.filter_offset)
            noise = noise * noise_mask

        def denoiser_override(n):
            return k_diffusion.sampling.get_sigmas_polyexponential(n, 0.01, 7, 0.5, devices.device)

        self.p.sampler_noise_scheduler_override = denoiser_override
        self.p.batch_size = 1
        samples = self.sampler.sample_img2img(self.p, sample.to(devices.dtype), noise, self.cond, self.uncond,
                                              steps=self.config.steps, image_conditioning=image_conditioning
                                              ).to(devices.dtype_vae)
        devices.torch_gc()
        self.p.iteration += 1
        decoded_sample = processing.decode_first_stage(shared.sd_model, samples)
        if math.isnan(decoded_sample.min()):
            devices.torch_gc()
            samples = torch.clamp(samples, -3, 3)
            decoded_sample = processing.decode_first_stage(shared.sd_model, samples)
        decoded_sample = torch.clamp((decoded_sample + 1.0) / 2.0, min=0.0, max=1.0).squeeze()
        x_sample = 255. * np.moveaxis(decoded_sample.cpu().numpy(), 0, 2)
        x_sample = x_sample.astype(np.uint8)
        image = Image.fromarray(x_sample)
        return image
