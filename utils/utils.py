from einops import rearrange
import numpy
from torch.utils.data import DataLoader
import torch
import torchaudio
from scipy.cluster.vq import kmeans
import constants as cst
import soundfile as sf

def compute_final_output_dim(input_dim, kernel_sizes, paddings, dilations, strides, num_convs):
    for i in range(num_convs):
        if i == 0:
            emb_sample_len = compute_output_dim_conv(input_dim=input_dim,
                                                        kernel_size=kernel_sizes[i],
                                                        padding=paddings[i],
                                                        dilation=dilations[i],
                                                        stride=strides[i])
        else:
            emb_sample_len = compute_output_dim_conv(input_dim=emb_sample_len,
                                                        kernel_size=kernel_sizes[i],
                                                        padding=paddings[i],
                                                        dilation=dilations[i],
                                                        stride=strides[i])
    return emb_sample_len


def compute_output_dim_convtranspose(input_dim, kernel_size, padding, dilation, stride):
    return (input_dim - 1) * stride - 2 * padding + dilation * (kernel_size - 1) + 1


def compute_output_dim_conv(input_dim, kernel_size, padding, dilation, stride):
    return (input_dim + 2 * padding - dilation * (kernel_size - 1) - 1) / stride + 1


def compute_mean_std(train_set, batch_size, num_workers, shuffle):
    train_dataloader = DataLoader(train_set, batch_size=batch_size, shuffle=shuffle, num_workers=num_workers, pin_memory=False)
    mean = 0.0
    std = 0.0
    num_samples = 0

    for batch in train_dataloader:
        data = torch.sum(batch, dim=-2)
        batch_size = data.size(0)
        data = data.view(batch_size, data.size(1), -1)
        mean += torch.mean(data, dim=1).sum(0)
        std += torch.std(data, dim=1).sum(0)
        num_samples += batch_size
        if num_samples % 10000 == 0:
            print(num_samples)
    print("final num smaples: ", num_samples)
    mean /= num_samples
    std /= num_samples

    return mean, std


def save_audio(waveform, filename, is_generated=False):
    if isinstance(waveform, numpy.ndarray):
        waveform = torch.tensor(waveform)
    waveform = waveform.cpu().detach()
    if waveform.ndim == 2:
        if (waveform.shape[0] > waveform.shape[1]):
            waveform = waveform.T
    else:
        waveform = waveform.unsqueeze(0)
    # Save as WAV
    if is_generated:
        torchaudio.save(cst.GEN_DIR + '/' + filename +'.wav', waveform, cst.SAMPLE_RATE)
    else:
        torchaudio.save(cst.RECON_DIR + '/' + filename +'.wav', waveform, cst.SAMPLE_RATE)


def compute_centroids(model, train_set):
    list = []
    for i in range(50):
        list.append(torch.sum(train_set.__getitem__(i), dim=-2))
    list = torch.stack(list).to(cst.DEVICE, torch.float32)
    list = rearrange(list, 'b w -> b 1 w').contiguous()
    z_e = model.AE.encoder(list)
    z_e = rearrange(z_e, 'b w c -> (b w) c').contiguous()
    centroids, _ = kmeans(z_e.detach().cpu(), model.AE.codebook_length, iter=100)
    print(centroids.shape)
    return centroids

def is_silent(signal: torch.Tensor, silence_threshold: float = 0.000215) -> bool:
    num_samples = signal.shape[-1]
    return torch.linalg.norm(signal) / num_samples < silence_threshold