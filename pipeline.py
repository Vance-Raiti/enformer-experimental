import torch
from basenji import BasenjiDataset
import wandb

import os
from os.path import exists, join,dirname
import sys
from random import choice

from enformer.metrics import MeanPearsonCorrCoefPerChannel as MPCCPC
from enformer.modeling_enformer import Enformer
from torch.optim.lr_scheduler import LambdaLR
import math

from henf import Enformer as HEnformer
from henf2 import Enformer as HEnformer2

from torch import _dynamo
_dynamo.config.suppress_errors = True

from einops._torch_specific import allow_ops_in_compiled_graph
allow_ops_in_compiled_graph()

ename = dirname(__file__)
checkpoints = join(ename,'checkpoints')

rid = sys.argv[1]
checkpoint = join(checkpoints,rid+'.pt')


sd = torch.load(checkpoint)
config = sd['config']
lr = config['lr']
debug = config['debug']
n_epochs = config['n_epochs']
epoch = config['epoch']
config['epoch'] += 1
arch = config['arch']

if 'sequence_length' in config:
    sequence_length = config['sequence_length']
else:
    sequence_length = 196608


if arch == 'h-enformer':
    arch = HEnformer
elif arch == 'h-enformer-2':
    arch = HEnformer2
else:
    arch = Enformer

n_train = 34000
n_valid = 2000



model = arch.from_hparams(
    **config['model_kwargs'],
)

loss = torch.nn.PoissonNLLLoss(log_input=False)

mpccpc = MPCCPC(n_channels = 5313).to('cuda')

train_data, valid_data = [
    torch.utils.data.DataLoader(
        BasenjiDataset(**config['dataset_kwargs']),
        batch_size=1,
        num_workers=0,
    )
    for split in ['train','valid']
]
if debug:
    os.environ['WANDB_MODE'] = 'disabled'
else:
    os.environ['WANDB_SILENT']='true'
    os.environ['WANDB_CONSOLE']='off'


optimizer = torch.optim.AdamW(
    model.parameters()|**config['optimizer_kwargs']
)

class linear_warmup_cosine_decay(LambdaLR):
    def __init__(
            self,
            warmup: int,
            N: int,
            **kwargs,
        ):
        def lwcd(n):
            if n < warmup:
                return n/warmup
            theta = math.pi*(warmup-n)/(N-warmup)
            return 0.5*math.cos(theta)+0.5
        super().__init__(lr_lambda=lwcd,**kwargs)

lr_schedule = linear_warmup_cosine_decay(optimizer=optimizer,warmup=int(9e4),N=n_train*n_epochs)
scaler = torch.cuda.amp.GradScaler()


if not config['debug']:
    model = torch.compile(model)

if epoch > 0:
    sd = torch.load(join(checkpoints,rid+'.pt'))
    model.load_state_dict(sd['model'])
    optimizer.load_state_dict(sd['optimizer'])
    lr_schedule.load_state_dict(sd['lr_schedule'])
    

model = model.to('cuda')

def optimizer_to(optim,device):
    for param in optim.state.values():
        if isinstance(param, torch.Tensor):
            param.data = param.data.to(device)
            if param._grad is not None:
                param._grad.data = param._grad.to(device)
        elif isinstance(param, dict):
            for subparam in param.values():
                if isinstance(subparam, torch.Tensor):
                    subparam.data = subparam.data.to(device)
                    if subparam._grad is not None:
                        subparam._grad.data = subparam._grad.data.to(device)

optimizer_to(optimizer,'cuda')

wandb.init(
    project = 'enformer-vance',
    id = rid,
    config = config,
    resume = True,
)



model.train()
for it, data in enumerate(train_data):
    x = data['features'].to('cuda')
    y = data['targets'].to('cuda')
    lr_schedule.step()
    with torch.autocast('cuda'):
        y_hat = model(x)
        if y_hat is None:
            continue
        l = loss(y_hat,y)
        scaler.scale(l).backward()
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 0.2)
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad()
        with torch.no_grad():
            mpccpc.update(y_hat,y)
            corr_coef = mpccpc.compute().mean()
    
    log = {
        'train/correlation': corr_coef.item(),
        'train/loss': l.item(),
        'iteration': it+n_train*epoch,
        'train/lr': lr_schedule.get_last_lr()[0],
    }
    print(log)
    wandb.log(log)
mpccpc.reset()

model.eval()
for it, data in enumerate(valid_data):
    x = data['features'].to('cuda')
    y = data['targets'].to('cuda')
    with torch.autocast('cuda'), torch.no_grad():
        y_hat = model(x)
        if y_hat is None:
            continue
        l = loss(y_hat,y)
        mpccpc.update(y_hat,y)
        corr_coef = mpccpc.compute().mean()
    
    log = {
        'valid/correlation': corr_coef.item(),
        'valid/loss': l.item(),
        'iteration': it+n_valid*epoch,
    }
    print(log)
    wandb.log(log)

wandb.finish()

model = model.to('cpu')
optimizer_to(optimizer,'cpu')


torch.save(
    {
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict(),
        'lr_schedule': lr_schedule.state_dict(),
        'config': config
    },
    checkpoint
)

if epoch == n_epochs:
    print('training done!')
    exit()

if debug:
    cmd = f'source job.sh {rid}'
else:
    cmd = f'qsub -P aclab -o logs/{rid}.log -e logs/{rid}.log -l gpus=1 -N {rid} -pe omp 32 -l gpu_c=7.0 -l h_rt=11:00:00 job.sh {rid}'
print(cmd)
os.system(cmd)
