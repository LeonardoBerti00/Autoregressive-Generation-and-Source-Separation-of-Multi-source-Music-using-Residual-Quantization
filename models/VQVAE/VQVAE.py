import random
import numpy as np
import torch
from einops import rearrange
from torch_ema import ExponentialMovingAverage
from config import Configuration
from constants import LearningHyperParameter
from torch import nn
from torch.nn import functional as F
from torch.linalg import vector_norm
import constants as cst
from torchmetrics.audio import ScaleInvariantSignalNoiseRatio
from scipy.cluster.vq import kmeans
import torchaudio.transforms as T

from models.Encoders import Encoder
from models.Decoders import Decoder
from utils.utils import compute_final_output_dim


class VQVAE(nn.Module):
    def __init__(self, config: Configuration):
        super().__init__()

        self.init_kmeans = config.HYPER_PARAMETERS[LearningHyperParameter.INIT_KMEANS]
        self.commitment_loss_weight = config.HYPER_PARAMETERS[LearningHyperParameter.COMMITMENT_LOSS_WEIGHT]
        self.IS_DEBUG = config.IS_DEBUG
        self.codebook_length = config.HYPER_PARAMETERS[LearningHyperParameter.CODEBOOK_LENGTH]
        self.is_training = config.IS_TRAINING
        self.recon_time_terms = []
        self.multi_spectral_recon_losses = []
        self.commitment_losses = []
        self.si_snr = ScaleInvariantSignalNoiseRatio()
        self.ema = ExponentialMovingAverage(self.parameters(), decay=0.999)

        num_convs = config.HYPER_PARAMETERS[LearningHyperParameter.NUM_CONVS]
        init_sample_len = cst.SAMPLE_LENGTH
        conv_setup = config.HYPER_PARAMETERS[LearningHyperParameter.CONV_SETUP]
        paddings = config.HYPER_PARAMETERS[LearningHyperParameter.PADDINGS][conv_setup]
        dilations = config.HYPER_PARAMETERS[LearningHyperParameter.DILATIONS][conv_setup]
        strides = config.HYPER_PARAMETERS[LearningHyperParameter.STRIDES][conv_setup]
        kernel_sizes = config.HYPER_PARAMETERS[LearningHyperParameter.KERNEL_SIZES][conv_setup]
        hidden_channels = config.HYPER_PARAMETERS[LearningHyperParameter.HIDDEN_CHANNELS][conv_setup]

        emb_sample_len = compute_final_output_dim(
            input_dim = init_sample_len,
            kernel_sizes = kernel_sizes,
            paddings = paddings,
            dilations = dilations,
            strides = strides,
            num_convs = num_convs
        )
        self.latent_dim = hidden_channels[-1]
        self.codebook = nn.Embedding(self.codebook_length, self.latent_dim)
        self.codebook.weight.data.uniform_(-1 / self.codebook_length, 1 / self.codebook_length)

        self.encoder = Encoder(
                input_size=init_sample_len,
                audio_srcs=len(cst.STEMS),
                hidden_channels=hidden_channels,
                kernel_sizes=kernel_sizes,
                strides=strides,
                paddings=paddings,
                dilations=dilations,
                lstm_layers=config.HYPER_PARAMETERS[LearningHyperParameter.LSTM_LAYERS],
                num_convs=num_convs,
                res_type=config.HYPER_PARAMETERS[LearningHyperParameter.RES_TYPE],
            )
        self.decoder = Decoder(
                audio_srcs=len(cst.STEMS),
                hidden_channels=hidden_channels,
                kernel_sizes=kernel_sizes,
                strides=strides,
                paddings=paddings,
                dilations=dilations,
                lstm_layers=config.HYPER_PARAMETERS[LearningHyperParameter.LSTM_LAYERS],
                batch_size=config.HYPER_PARAMETERS[LearningHyperParameter.BATCH_SIZE],
                emb_sample_len=emb_sample_len,
                num_convs=num_convs,
                res_type=config.HYPER_PARAMETERS[LearningHyperParameter.RES_TYPE],
            )
        self.codebook_freq = torch.zeros(self.codebook_length, device=cst.DEVICE)

        # initiliazation to compute the multi spectogram loss
        # a list of powers of two for the window sizes of each MelSpectrogram transform
        multi_spectral_window_powers = cst.MULTI_SPECTRAL_WINDOW_POWERS 
        # hyperparameter that controls the weight of the multi spectogram loss in the total loss function
        self.multi_spectral_recon_loss_weight = config.HYPER_PARAMETERS[LearningHyperParameter.MULTI_SPECTRAL_RECON_LOSS_WEIGHT] 
        num_transforms = len(multi_spectral_window_powers) 
        multi_spectral_n_ffts = ((cst.MULTI_SPECTRAL_N_FFTS,) * num_transforms)
        multi_spectral_n_mels = ((cst.MULTI_SPECTRAL_N_MELS,) * num_transforms) 
        self.mel_spec_transforms = []
        self.mel_spec_recon_alphas = []
        for powers, n_fft, n_mels in zip(multi_spectral_window_powers, multi_spectral_n_ffts, multi_spectral_n_mels):
            win_length = 2 ** powers 
            # calculate the alpha value for the current transform, which is used to scale the output of the MelSpectrogram
            alpha = (win_length / 2) ** 0.5 
            # calculate the number of frequency bins for the current transform
            calculated_n_fft = max(n_fft, win_length) 
            # create a MelSpectrogram transform object from the PyTorch library, using the calculated parameters and some constants
            melspec_transform = T.MelSpectrogram(
                sample_rate = cst.SAMPLE_RATE,
                n_fft = calculated_n_fft,
                win_length = win_length,
                hop_length = win_length // 4,
                n_mels = n_mels,
            ).to(cst.DEVICE)

            self.mel_spec_transforms.append(melspec_transform)
            self.mel_spec_recon_alphas.append(alpha)



    def forward(self, x, is_train, batch_idx):
        #adding channel dimension
        x = rearrange(x, 'b l-> b 1 l')

        z_e, encoding_indices = self.encode(x)
        # if it is the first iteration we initialize the codebook with kmeans
        if self.init_kmeans:
            with torch.no_grad():
                centroids, _ = kmeans(z_e.detach().cpu(), self.codebook_length, iter=100)
                self.codebook.weight.data[0:centroids.shape[0]] = torch.tensor(centroids, device=self.device, dtype=torch.float32).contiguous()
                self.init_kmeans = False
        
        if batch_idx % 50 == 0:
            # reinitialize all the codebook vector that have less than 1 value
            # with a random vector from the encoder output
            for index in range(self.codebook_length):
                if self.codebook_freq[index] < 1:
                    i = random.randint(0, z_e.shape[0])
                    self.codebook.weight.data[index] = z_e[i]
            # reinitialize the dictionary
            self.codebook_freq = torch.zeros(self.codebook_length, device=cst.DEVICE)

        recon, z_q = self.decode(z_e, encoding_indices, x.shape[0], is_train)

        return recon, z_q, z_e


    def encode(self, x):
        x = self.encoder(x)
        z_e = rearrange(x, 'b w c -> (b w) c').contiguous()
        dist = torch.cdist(z_e, self.codebook.weight)
        _, encoding_indices = torch.min(dist, dim=1)
        #update the dictionary of indices with the frequency of each index of the codebook
        self.codebook_freq[encoding_indices] += 1
        return z_e, encoding_indices


    def decode(self, z_e, encoding_indices, batch_size, is_train):
        encodings = F.one_hot(encoding_indices, self.codebook_length).float()
        z_q = torch.matmul(encodings, self.codebook.weight)

        if is_train:
            # straight-through gradient estimation: we attach z_q to the computational graph
            # to subsequently pass the gradient unaltered from the decoder to the encoder
            # whatever the gradients you get for the quantized variable z_q they will just be copy-pasted into z_e
            z_q = z_e + (z_q - z_e).detach()

        z_q_rearranged = rearrange(z_q, '(b l) c -> b l c', b=batch_size).contiguous()
        recon = self.decoder(z_q_rearranged)
        return recon, z_q


    def loss(self, input, recon, z_q, z_e):
        # Commitment loss is the mse between the quantized latent vector and the encoder output
        q_latent_loss = F.mse_loss(z_e.detach(), z_q)      # we train the codebook
        e_latent_loss = F.mse_loss(z_e, z_q.detach())      # we train the encoder
        commitment_loss = q_latent_loss + e_latent_loss
        # Reconstruction loss is the mse between the input and the reconstruction
        recon_time_term = F.mse_loss(input, recon)
        multi_spectral_recon_loss = 0
        for src in range(len(cst.STEMS)):
            for mel_transform, alpha in zip(self.mel_spec_transforms, self.mel_spec_recon_alphas):
                orig_mel, recon_mel = map(mel_transform, (input[:, src, :], recon[:, src, :]))
                log_orig_mel, log_recon_mel = torch.log(orig_mel), torch.log(recon_mel)

                l1_mel_loss = (orig_mel - recon_mel).abs().sum(dim = -2).mean()
                l2_log_mel_loss = vector_norm(log_orig_mel - log_recon_mel, dim = -2).mean()

                multi_spectral_recon_loss = multi_spectral_recon_loss + l1_mel_loss + l2_log_mel_loss
        self.recon_time_terms.append(recon_time_term.item())
        self.multi_spectral_recon_losses.append(multi_spectral_recon_loss.item())
        self.commitment_losses.append(commitment_loss.item())
        print(f"Reconstruction time term: {recon_time_term.item()}")
        print(f"Multi spectral reconstruction loss: {multi_spectral_recon_loss.item()}")
        print(f"Commitment loss: {commitment_loss.item()}")
        return recon_time_term + commitment_loss + self.multi_spectral_recon_loss_weight*multi_spectral_recon_loss