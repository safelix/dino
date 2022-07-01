import time

import pytorch_lightning as pl
from pytorch_lightning.loggers import WandbLogger
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import MNIST

from configuration import CONSTANTS as C
from configuration import Configuration, create_encoder, create_optimizer
from dino import *
from probing import LinearProbingCallback
from tracking import (HParamTracker, MetricsTracker, ParamTracker,
                      PerCropEntropyTracker)

wandb_logger = WandbLogger(project="DINO_MNIST")

import my_utils as U


def main(config:Configuration):
    # Fix random seed
    if config.seed is None:
        config.seed = int(time.time())

    # Multi-Crop Augmentation
    MC_SPEC = [
            {'name':'global1', 'out_size':128, 'min_scale':0.6, 'max_scale':1.0, 'teacher':True, 'student':True},
            {'name':'global2', 'out_size':128, 'min_scale':0.6, 'max_scale':1.0, 'teacher':True, 'student':True},
            {'name':'local1', 'out_size':96, 'min_scale':0.05, 'max_scale':0.4, 'teacher':False, 'student':True},
            {'name':'local2', 'out_size':96, 'min_scale':0.05, 'max_scale':0.4, 'teacher':False, 'student':True},
            {'name':'local3', 'out_size':96, 'min_scale':0.05, 'max_scale':0.4, 'teacher':False, 'student':True},
            {'name':'local4', 'out_size':96, 'min_scale':0.05, 'max_scale':0.4, 'teacher':False, 'student':True},
        ]

    
    self_trfm = transforms.Compose([ # self-training
                    transforms.Lambda(lambda img: img.convert('RGB')),
                    transforms.ToTensor()
                ])
    eval_trfm = transforms.Compose([ # evaluation
                    transforms.Resize(size=128),
                    self_trfm
                ])
                    
    mc = MultiCropAugmentation(MC_SPEC, per_crop_transform=self_trfm)

    # Data Loading
    config.n_classes = 10
    self_train_set = MNIST(root=C.DATA_DIR, train=True, transform=mc)
    self_valid_set = MNIST(root=C.DATA_DIR, train=False, transform=mc)
    eval_train_set = MNIST(root=C.DATA_DIR, train=True, transform=eval_trfm)
    eval_valid_set = MNIST(root=C.DATA_DIR, train=False, transform=eval_trfm)
    print(f'Data loaded: size(train)={len(self_train_set)}, size(valid)={len(self_valid_set)}')


    # TODO: move after automatic gpu selection
    gpu_args = {} if config.force_cpu else {'num_workers':config.n_workers, 'pin_memory':True} 
    self_train_dl = DataLoader(dataset=self_train_set, batch_size=config.bs_train, **gpu_args)
    self_valid_dl = DataLoader(dataset=self_valid_set, batch_size=config.bs_train, **gpu_args)
    eval_train_dl = DataLoader(dataset=eval_train_set, batch_size=config.bs_eval, **gpu_args)
    eval_valid_dl = DataLoader(dataset=eval_valid_set, batch_size=config.bs_eval, **gpu_args)
   
    # Model Setup
    enc = create_encoder(config)
    head = DINOHead(config.embed_dim, config.out_dim, 
            hidden_dims=config.hid_dims, 
            l2_bottleneck_dim=config.bot_dim, 
            use_bn=config.use_bn,
            act_fn=config.mlp_act)
    model = DINOModel(enc, head)


    # DINO Setup
    dino = DINO(mc=mc, model=model,
                t_mode = config.t_mode,
                t_mom  = config.t_mom,
                t_cmom = config.t_cmom,
                t_temp = config.t_temp,
                s_temp = config.s_temp,
                opt = create_optimizer(config),
                opt_lr = config.opt_lr,
                opt_wd = config.opt_wd)

    # Tracking Logic
    probing_cb = LinearProbingCallback(
        encoders={'student': dino.student.enc,
                'teacher': dino.teacher.enc},
        embed_dim=config.embed_dim, 
        n_classes=config.n_classes,
        train_dl=eval_train_dl,
        valid_dl=eval_valid_dl,
        probe_every=config.probe_every,
        probing_epochs=config.probing_epochs,
    )

    callbacks = [
            MetricsTracker(), 
            PerCropEntropyTracker(), 
            HParamTracker(),
            ParamTracker(track_init=True),
            probing_cb,
        ]

    # Training
    trainer = pl.Trainer(
        # training dynamics
        max_epochs=config.n_epochs,
        gradient_clip_val=config.clip_grad,
        callbacks=callbacks,

        # logging
        logger=wandb_logger,
        log_every_n_steps=config.log_every,

        # acceleration
        accelerator='cpu' if config.force_cpu else 'gpu',
        devices=None if config.force_cpu else 1,
        #auto_select_gpus=True,
        gpus=[2],

        # performance
        benchmark=True,
        deterministic=False,

        # debugging
        #limit_train_batches=2,
        #limit_val_batches=2,
        )

    trainer.fit(model=dino, 
                train_dataloaders=self_train_dl,
                val_dataloaders=self_valid_dl)


if __name__ == '__main__':

    config = Configuration.parse_cmd()
    print(f'Starting experiment with configuration: \n {config}', flush=True)
    main(config)
