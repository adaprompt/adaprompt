import argparse, os, sys, glob
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
import re

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model

# copied from img2img.py
def load_img(path, h, w):
    image = Image.open(path).convert("RGB")
    w0, h0 = image.size
    print(f"loaded input image of size ({w0}, {h0}) from {path}")
    w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32
    image = image.resize((w, h), resample=Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="a painting of a virus monster playing guitar",
        help="the prompt to render"
    )
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/txt2img-samples"
    )
    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    parser.add_argument(
        "--skip_save",
        action='store_true',
        help="do not save individual samples. For speed measurements.",
    )
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
    )
    parser.add_argument(
        "--laion400m",
        action='store_true',
        help="uses the LAION400M model",
    )
    parser.add_argument(
        "--fixed_code",
        action='store_true',
        help="if enabled, uses the same starting code across samples ",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=2,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=3,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=7.5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="models/ldm/stable-diffusion-v1/model.ckpt",
        help="path to checkpoint of model",
    )    
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )


    parser.add_argument(
        "--embedding_paths", 
        nargs="*", 
        type=str, default=None,
        help="Paths to a pre-trained embedding manager checkpoint")
    parser.add_argument(
        "--subj_scale",
        type=float,
        default=1.0,
        help="Scale of the subject embedding",
    )

    parser.add_argument("--lasr_emb_weight",
        type=float, default=-1,
        help="Weight of lasr embeddings (in contrast to static embeddings)")

    parser.add_argument(
        "--init_img",
        type=str,
        default=None,
        help="path to the initial image",
    )  
    # Anything between 0 and 1 will cause blended images.
    parser.add_argument(
        "--mask_weight",
        type=float,
        default=0.0,
        help="Weight of the initial image",
    )
    # No preview
    parser.add_argument(
        "--no_preview",
        action='store_true',
        help="do not preview the image",
    )

    parser.add_argument('--gpu', type=str,  default='1', help='ID of GPU to use')

    opt = parser.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = opt.gpu

    if opt.laion400m:
        print("Falling back to LAION 400M model...")
        opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        opt.ckpt = "models/ldm/text2img-large/model.ckpt"
        opt.outdir = "outputs/txt2img-samples-laion400m"

    seed_everything(opt.seed)

    config = OmegaConf.load(f"{opt.config}")
    model  = load_model_from_config(config, f"{opt.ckpt}")
    if opt.embedding_paths is not None:
        model.embedding_manager.load(opt.embedding_paths)
    model.embedding_manager.subj_scale  = opt.subj_scale
    if opt.lasr_emb_weight >= 0:
        model.embedding_manager.lasr_emb_weight = opt.lasr_emb_weight

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model  = model.to(device)

    if opt.plms:
        sampler = PLMSSampler(model)
    else:
        sampler = DDIMSampler(model)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    batch_size = opt.n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    if opt.init_img is not None:
        assert opt.fixed_code is False
        init_img = load_img(opt.init_img, opt.H, opt.W)
        init_img = init_img.repeat([opt.n_samples, 1, 1, 1]).to(device)
        # move init_img to latent space
        x0      = model.get_first_stage_encoding(model.encode_first_stage(init_img))  
        mask    = torch.ones_like(x0) * opt.mask_weight
    else:
        x0      = None
        mask    = None

    start_code = None
    if opt.fixed_code:
        start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

    precision_scope = autocast if opt.precision=="autocast" else nullcontext
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                for n in trange(opt.n_iter, desc="Sampling"):
                    for prompts in tqdm(data, desc="data"):
                        uc = None
                        if opt.scale != 1.0:
                            uc = model.get_learned_conditioning(batch_size * [""])
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)
                        c = model.get_learned_conditioning(prompts)
                        shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                        # When lasr embedding is used, c is a tuple of (cond, lasr_embedder).
                        # When both unconditional and conditional guidance are passed to ddim sampler, 
                        # lasr_embedder of the conditional guidance is applied on both 
                        # unconditional and conditional embeddings within UNetModel.forward(). 
                        # But since unconditional prompt doesn't contain the placeholder token,
                        # lasr_embedder won't change the unconditional embedding uc.
                        samples_ddim, _ = sampler.sample(S=opt.ddim_steps,
                                                         conditioning=c,
                                                         batch_size=opt.n_samples,
                                                         shape=shape,
                                                         verbose=False,
                                                         unconditional_guidance_scale=opt.scale,
                                                         unconditional_conditioning=uc,
                                                         eta=opt.ddim_eta,
                                                         x0=x0,
                                                         mask=mask,
                                                         x_T=start_code)

                        x_samples_ddim = model.decode_first_stage(samples_ddim)
                        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)

                        if not opt.skip_save:
                            for x_sample in x_samples_ddim:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                Image.fromarray(x_sample.astype(np.uint8)).save(
                                    os.path.join(sample_path, f"{base_count:05}.jpg"))
                                base_count += 1

                        if not opt.skip_grid:
                            all_samples.append(x_samples_ddim)

                if not opt.skip_grid:
                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    # logs/name2022-12-27T23-03-14_name/checkpoints/embeddings_gs-1200.pt
                    date_mat = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})_([^\/]+)", opt.embedding_paths[0])
                    date_sig = date_mat.group(1)
                    ckpt_postfix = date_mat.group(2)
                    iter_mat = re.search(r"(\d+).pt", opt.embedding_paths[0])
                    iter_sig = iter_mat.group(1)

                    embedding_sig = "-".join([date_sig, iter_sig, f"scale{opt.scale:.1f}"])

                    grid_filepath = os.path.join(outpath, f'{ckpt_postfix}-{prompt.replace(" ", "-")}-{embedding_sig}.jpg')
                    if os.path.exists(grid_filepath):
                        grid_filepath = os.path.join(outpath, f'{ckpt_postfix}-{prompt.replace(" ", "-")}-{embedding_sig}-{grid_count}.jpg')
                    Image.fromarray(grid.astype(np.uint8)).save(grid_filepath)
                    grid_count += 1

                toc = time.time()

    if not opt.skip_grid or not opt.no_preview:
        print(f"Your samples are ready and waiting for you here: \n{grid_filepath}")
        os.spawnvp(os.P_NOWAIT, "gpicview", [ "gpicview", os.path.abspath(grid_filepath) ])
    else:
        print(f"Your samples are ready and waiting for you here: \n{outpath}")

if __name__ == "__main__":
    main()
