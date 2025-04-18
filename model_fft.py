import os
import torch
import torch.nn as nn
import math
import torch.fft
import torch.nn.functional as F
import pywt

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def trunc_normal(tensor, mean=0., std=1., a=-2., b=2.):  # for positional embedding - borrowed from Meta
    def norm_cdf(x):  # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        print("mean is more than 2 std from [a, b] in nn.init.trunc_normal\nThe distribution of values may be incorrect.")

    with torch.no_grad():  # Values are generated by using a truncated uniform distribution and then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

def apply_wavelet_transform(x, wavelet='morl', scales=None):
    """
    Apply Continuous Wavelet Transform (CWT) to EEG signals.
    
    Args:
        x: torch.Tensor of shape [batch_size, channels, time_points]
        wavelet: Wavelet function to use (e.g., 'morl', 'cmor', 'db4')
        scales: List or array of scales for wavelet transform

    Returns:
        torch.Tensor of shape [batch_size, channels, scales, time_points] (Magnitude of CWT)
    """
    batch_size, num_channels, num_timepoints = x.shape

    # Default scales if not provided
    if scales is None:
        scales = torch.arange(1, min(128, num_timepoints // 2))  # Adjust scales based on time resolution

    transformed_signals = []
    
    for batch_idx in range(batch_size):
        channel_transforms = []
        
        for ch in range(num_channels):
            signal = x[batch_idx, ch].cpu().numpy()  # Convert to numpy for PyWavelets

            # Compute CWT
            coeffs, freqs = pywt.cwt(signal, scales.numpy(), wavelet)

            # Convert back to torch tensor and use magnitude
            coeffs = torch.tensor(abs(coeffs), dtype=x.dtype).to(x.device)  # Shape: [scales, time]

            channel_transforms.append(coeffs.unsqueeze(0))  # Add channel dimension
        
        transformed_signals.append(torch.cat(channel_transforms, dim=0).unsqueeze(0))  # Add batch dimension

    return torch.cat(transformed_signals, dim=0)  # Shape: [batch_size, channels, scales, time]

def apply_fft(x):
    """
    Apply FFT to EEG signals.
    Args:
        x: torch.Tensor of shape [batch_size, channels, time_points]
    Returns:
        torch.Tensor of shape [batch_size, channels, time_points] (Magnitude of FFT)
    """
    fft_result = torch.fft.rfft(x, dim=-1)  # Compute FFT along time dimension
    fft_magnitude = torch.abs(fft_result)  # Use magnitude (or keep complex values)
    return fft_magnitude


import torch
import torch.nn as nn

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, 
                 act_layer=nn.GELU, drop=0., dtype=torch.float32):
        super().__init__()
        
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.net = nn.Sequential(
            nn.Linear(in_features, hidden_features, dtype=dtype),
            act_layer(),
            nn.Dropout(drop),
            nn.Linear(hidden_features, out_features, dtype=dtype),
            nn.Dropout(drop)
        )

    def forward(self, x):
        return self.net(x)



# class GenericTFB(nn.Module):
#     def __init__(self, emb_size, num_heads, dtype):
#         super(GenericTFB, self).__init__()

#         self.M_size1 = emb_size  # -> D
#         self.dtype = dtype
#         self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
#         self.Dh = int(self.M_size1 / self.hA)  # Dh is the quotient computed by D/A and denotes the dimension number of three vectors.

#         self.Wqkv = nn.Parameter(torch.randn((3, self.hA, self.Dh, self.M_size1), dtype=self.dtype))
#         self.Wo = nn.Parameter(torch.randn(self.M_size1, self.M_size1, dtype=self.dtype))

#         self.lnorm = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for dimension D
#         self.lnormz = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for z
#         self.mlp = Mlp(in_features=self.M_size1, hidden_features=int(self.M_size1 * 4), act_layer=nn.GELU, dtype=self.dtype)  # mlp_ratio=4

#     def forward(self, x, savespace):
#         #print('Input x shape:', x.shape)  # Expected: [batch_size, channels, timesteps]
#         #print('Input savespace shape:', savespace.shape)  # Expected: [batch_size, channels, timesteps, embedding_dim]

#         # Initialize spaces with batch size included
#         batch_size = x.shape[0]
#         qkvspace = torch.zeros(
#             batch_size, 3, x.shape[3], x.shape[1] + 1, self.hA, self.Dh, dtype=self.dtype
#         ).to(device)  # Q, K, V
#         atspace = torch.zeros(
#             batch_size, x.shape[3], self.hA, x.shape[1] + 1, x.shape[1] + 1, dtype=self.dtype
#         ).to(device)
#         imv = torch.zeros(
#             batch_size, x.shape[3], x.shape[1] + 1, self.hA, self.Dh, dtype=self.dtype
#         ).to(device)

#         #print('qkv space:', qkvspace.shape)
#         #print('atspace:', atspace.shape)
#         #print('imv:', imv.shape)

#         # Compute Q, K, V using einsum with batch dimension
#         qkvspace = torch.einsum('xhdm,bijm -> bxijhd', self.Wqkv, self.lnorm(savespace))
#         #print('qkv space after einsum:', qkvspace.shape)  # [batch_size, 3, timesteps, channels+1, num_heads, Dh]

#         # Compute attention scores
#         atspace = (
#             qkvspace[:, 0].clone().transpose(2, 3) / math.sqrt(self.Dh)
#         ) @ qkvspace[:, 1].clone().transpose(2, 3).transpose(-2, -1)
#         #print('atspace:', atspace.shape)  # [batch_size, timesteps, num_heads, channels+1, channels+1]

#         # Compute intermediate vectors
#         imv = (
#             atspace.clone() @ qkvspace[:, 2].clone().transpose(2, 3)
#         ).transpose(2, 3)
#         #print('imv:', imv.shape)  # [batch_size, timesteps, channels+1, num_heads, Dh]

#         # Compute new z (output)
#         savespace = torch.einsum(
#             'nm,bijn -> bijn', self.Wo, imv.clone().reshape(batch_size, x.shape[3], x.shape[1] + 1, self.M_size1)
#         ) + savespace
        
#         # savespace = torch.einsum('nm,ijm -> ijn', self.Wo, imv.clone().reshape(x.shape[2], x.shape[0] + 1, self.M_size1)) + savespace

#         #print('savespace after einsum and addition:', savespace.shape)  # [batch_size, timesteps, channels+1, embedding_dim]

#         # Normalize and pass through MLP
#         savespace = self.mlp(self.lnormz(savespace)) + savespace
#         #print('savespace after normalization and MLP:', savespace.shape)  # [batch_size, timesteps, channels+1, embedding_dim]

#         return savespace

class GenericTFB(nn.Module):
    def __init__(self, num_heads, dtype=torch.float32):
        super(GenericTFB, self).__init__()
        self.num_heads = num_heads
        self.dtype = dtype

        # We'll dynamically define the rest inside `forward()`
        self._initialized = False  # Lazy init flag

    def _init_weights(self, embedding_dim, device):
        Dh = embedding_dim // self.num_heads
        assert embedding_dim % self.num_heads == 0, f"embedding_dim {embedding_dim} must be divisible by num_heads {self.num_heads}"

        self.embedding_dim = embedding_dim
        self.Dh = Dh

        self.Wqkv = nn.Parameter(torch.randn(3, self.num_heads, self.Dh, self.embedding_dim, dtype=self.dtype, device=device))
        self.Wo = nn.Parameter(torch.randn(self.embedding_dim, self.embedding_dim, dtype=self.dtype, device=device))

        self.mlp = Mlp(
            in_features=self.embedding_dim,
            hidden_features=4 * self.embedding_dim,
            out_features=self.embedding_dim,
            dtype=self.dtype
        ).to(device)

        self._initialized = True

    def forward(self, x, savespace):
        B, _, H, W = savespace.shape

        if not self._initialized:
            self._init_weights(W, savespace.device)

        # Dynamic layer norms
        lnorm = nn.LayerNorm(W).to(savespace.device)
        lnormz = nn.LayerNorm(W).to(savespace.device)

        savespace_norm = lnorm(savespace)

        # qkv projection: [3, heads, Dh, embedding_dim] x [B, I, J, M] -> [3, B, I, J, heads, Dh]
        qkvspace = torch.einsum('xhdm,bijm -> bxijhd', self.Wqkv, savespace_norm)
        q, k, v = qkvspace[0], qkvspace[1], qkvspace[2]  # [B, heads, I, J, Dh]

        attn_scores = torch.einsum('bxqhd,bxkhd->bxqhk', q, k) / (self.embedding_dim ** 0.5)
        attn_weights = torch.softmax(attn_scores, dim=-1)
        context = torch.einsum('bxqhk,bxkhd->bxqhd', attn_weights, v)  # [B, heads, Q, K, Dh]

        # Reshape context from [B, heads, Q, K, Dh] → [B, Q, K, heads * Dh]
        B, h, Q, K, Dh = context.shape
        context = context.permute(0, 2, 3, 1, 4).contiguous()  # [B, Q, K, heads, Dh]
        context = context.view(B, Q, K, h * Dh)  # [B, Q, K, embedding_dim]
        
        # Match shape to savespace for residual and projection
        context = torch.einsum('bijd,de->bije', context, self.Wo)

        z = lnormz(context + savespace)
        output = z + self.mlp(z)
        return output




class TemporalTFB(nn.Module):
    def __init__(self, emb_size, num_heads, avgf, dtype):
        super(TemporalTFB, self).__init__()

        self.avgf = avgf  # average factor (M)
        self.M_size1 = emb_size  # -> D
        self.dtype = dtype
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)  # Dh is the quotient computed by D/A and denotes the dimension number of three vectors.
        self.Wqkv = nn.Parameter(torch.randn((3, self.hA, self.Dh, self.M_size1), dtype=self.dtype))
        self.Wo = nn.Parameter(torch.randn(self.M_size1, self.M_size1, dtype=self.dtype))

        self.lnorm = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for dimension D
        self.lnormz = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for z
        self.mlp = Mlp(in_features=self.M_size1, hidden_features=int(self.M_size1 * 4), act_layer=nn.GELU, dtype=self.dtype)  # mlp_ratio=4

    def forward(self, x, savespace):
        # Initialize spaces with batch size included
        batch_size = x.shape[0]

        # Initialize spaces
        qkvspace = torch.zeros(batch_size, 3, self.avgf + 1, self.hA, self.Dh, dtype=self.dtype).to(device)  # Q, K, V
        atspace = torch.zeros(batch_size, self.hA, self.avgf + 1, self.avgf + 1, dtype=self.dtype).to(device)
        imv = torch.zeros(batch_size, self.avgf + 1, self.hA, self.Dh, dtype=self.dtype).to(device)

        #print(f"qkvspace initialized with shape: {qkvspace.shape}")
        #print(f"atspace initialized with shape: {atspace.shape}")
        #print(f"imv initialized with shape: {imv.shape}")

        # Compute Q, K, V using einsum
        #print("Computing Q, K, V using einsum...")
        #print("wqkv", self.Wqkv.shape)
        #print("savespace", self.lnorm(savespace).shape)
        qkvspace = torch.einsum('xhdm,bim -> bxihd', self.Wqkv, self.lnorm(savespace))  # Q, K, V
        #print(f"qkvspace after einsum computation: {qkvspace.shape}")

        # Compute attention scores
        #print("Computing attention scores...")
        atspace = (qkvspace[:, 0].clone().transpose(1, 2) / math.sqrt(self.Dh)) @ qkvspace[:, 1].clone().transpose(1, 2).transpose(-2, -1)
        #print(f"atspace after attention computation: {atspace.shape}")

        # Compute intermediate vectors
        #print("Computing intermediate vectors (imv)...")
        imv = (atspace.clone() @ qkvspace[:, 2].clone().transpose(1, 2)).transpose(1, 2)
        #print(f"imv after computation: {imv.shape}")

        # Update savespace with new Z
        #print("Updating savespace with new Z...")
        savespace = torch.einsum('nm,bim -> bin', self.Wo, imv.clone().reshape(batch_size, self.avgf + 1, self.M_size1)) + savespace
        #print(f"savespace updated with new Z: {savespace.shape}")

        # Normalize and pass through MLP
        #print("Normalizing savespace and passing through MLP...")
        savespace = self.mlp(self.lnormz(savespace)) + savespace
        #print(f"savespace after normalization and MLP: {savespace.shape}")
        return savespace


class ODCM(nn.Module):
    def __init__(self, input_channels, kernel_size=(3,3), dtype=torch.float32):
        super(ODCM, self).__init__()
        self.inpch = input_channels
        self.ksize = kernel_size  # 3x3 kernel for 2D processing
        self.ncf = 120  # Number of convolutional filters

        # First 2D convolution layer
        self.cvf1 = nn.Conv2d(
            in_channels=1,  
            out_channels=32, 
            kernel_size=self.ksize,
            padding=(1,1),  
            stride=(1,1),
        )

        # Second 2D convolution layer
        self.cvf2 = nn.Conv2d(
            in_channels=32, 
            out_channels=64, 
            kernel_size=self.ksize,
            padding=(1,1),
            stride=(1,1),
        )

        # Third 2D convolution layer
        self.cvf3 = nn.Conv2d(
            in_channels=64, 
            out_channels=128,  
            kernel_size=self.ksize,
            padding=(1,1),
            stride=(1,1),
        )

        # Activation function
        self.relu = nn.ReLU()

    def forward(self, x):
        """
        Input Shape: [batch_size, 1, scales (127), time_steps (531)]
        Expected Output Shape: [batch_size, 128, scales, time_steps] 
        """

        print(f"Input to ODCM: {x.shape}")  # Expected: [B, 1, 127, 531]

        # First conv
        x = self.relu(self.cvf1(x))
        print(f"After Conv2D Layer 1: {x.shape}")  # [B, 32, 127, 531]

        # Second conv
        x = self.relu(self.cvf2(x))
        print(f"After Conv2D Layer 2: {x.shape}")  # [B, 64, 127, 531]

        # Third conv
        x = self.relu(self.cvf3(x))
        print(f"After Conv2D Layer 3: {x.shape}")  # [B, 128, 127, 531]

        return x  # Output shape: [B, 128, 127, 531]




class RTM(nn.Module):
    def __init__(self, input_shape, num_blocks, num_heads, dtype):  
        super(RTM, self).__init__()

        if isinstance(input_shape, torch.Tensor):
            input_shape = tuple(input_shape.shape)

        self.num_blocks = num_blocks
        self.num_heads = num_heads
        self.dtype = dtype

        _, self.num_channels, self.height, self.width = input_shape
        self.embedding_dim = self.width  # For consistency with GenericTFB

        self.tfb = nn.ModuleList([
            GenericTFB(num_heads=self.num_heads, dtype=self.dtype)
            for _ in range(self.num_blocks)
        ])


    def forward(self, x):
        """
        Input: x of shape [B, C, H, W]
        Output: processed tensor of shape [B, 2, H, W] (or similar depending on your stack)
        """
        print(f"RTM received input: {x.shape}")  # e.g., [B, 128, 106, 510]
        B, C, H, W = x.shape

        # Permute for alignment: [B, H, W, C]
        x = x.permute(0, 2, 3, 1).to(self.dtype)
        print(f"RTM permuted input shape: {x.shape}")

        # Generate weight dynamically to match input shape
        weight = torch.randn(B, H, W, C, dtype=self.dtype, device=x.device)

        # Einsum self-attention-like multiplication
        savespace = torch.einsum('bhwc,bhwc->bhw', weight, x).unsqueeze(1)  # [B, 1, H, W]
        print(f"RTM savespace shape: {savespace.shape}")

        # Dynamic class token and bias
        cls = torch.zeros(B, 1, H, W, dtype=self.dtype, device=x.device)
        bias = torch.zeros(B, 2, H, W, dtype=self.dtype, device=x.device)

        # Append CLS token
        savespace = torch.cat((cls, savespace), dim=1)  # [B, 2, H, W]
        print(f"After cls concat: {savespace.shape}")

        # Add bias
        savespace = savespace + bias

        # Pass through transformer blocks
        for tfb in self.tfb:
            savespace = tfb(x, savespace)

        return savespace





class STM(nn.Module):  # Synchronous transformer module
    def __init__(self, input, num_blocks, num_heads, dtype):  # input -> # S x C x D
        super(STM, self).__init__()
        self.inputshape = input.transpose(2, 3).shape  # S x D x C (S x Le x C in the paper)
        self.M_size1 = self.inputshape[2]  # -> D
        self.dtype = dtype

        self.tK = num_blocks  # number of transformer blocks - K in the paper
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)  # Dh is the quotient computed by D/A and denotes the dimension number of three vectors.

        if self.M_size1 % self.hA != 0 or int(self.M_size1 / self.hA) == 0:
            print(f"ERROR 2 - STM : self.Dh = {int(self.M_size1 / self.hA)} != {self.M_size1}/{self.hA} \nTry with different num_heads")

        self.weight = nn.Parameter(torch.randn(self.inputshape[0],self.M_size1, self.inputshape[2], dtype=self.dtype))
        self.bias = nn.Parameter(torch.zeros(self.inputshape[0], self.inputshape[3], self.inputshape[1] + 1, self.M_size1, dtype=self.dtype))  # S x C x D
        self.cls = nn.Parameter(torch.zeros(self.inputshape[0], self.inputshape[3], 1, self.M_size1, dtype=self.dtype))
        trunc_normal(self.bias, std=.02)
        trunc_normal(self.cls, std=.02)
        self.tfb = nn.ModuleList([GenericTFB(self.M_size1, self.hA, self.dtype) for _ in range(self.tK)])

    def forward(self, x):  # S x C x D -> x
        #print("====STM Forward Pass Start ====")
        #print(f"Input shape (x): {x.shape} (expected: [batch_size, timesteps, channels])")

        # Transpose input to match expected dimensions
        x = x.transpose(2, 3)  # From [batch_size, timesteps, channels] -> [batch_size, channels, timesteps]
        #print(f"Transposed input shape: {x.shape} (expected: [batch_size, channels, timesteps])")

        # Initialize savespace
        #print("Initializing savespace...")
        savespace = torch.zeros(
            x.shape[3],  # timesteps
            x.shape[1] + 1,  # batch_size + 1 (to accommodate CLS token)
            self.M_size1,  # Embedding size
            dtype=self.dtype
        ).to(device)
        #print(f"Initialized savespace shape: {savespace.shape} (expected: [timesteps, batch_size + 1, embedding_dim])")

        # Perform einsum operation
        #print("Performing einsum operation to compute savespace...")
        #print(f"Weight shape: {self.weight.shape}")  # lm
        #print(f"x shape: {x.shape}")  # jmi
        savespace = torch.einsum('blm,bjmi -> bijl', self.weight, x)

        #print(f"savespace after einsum: {savespace.shape} (expected: [timesteps, batch_size, embedding_dim])")

        # Concatenate CLS token
        #print("Concatenating CLS token to savespace...")
        #print(f"CLS token shape: {self.cls.shape} (expected: [timesteps, 1, embedding_dim])")
        savespace = torch.cat((self.cls, savespace), dim=2)  # Concatenate along the batch dimension
        #print(f"savespace after concatenation: {savespace.shape} (expected: [timesteps, batch_size + 1, embedding_dim])")

        # Add bias to savespace
        #print("Adding bias to savespace...")
        #print(f"Bias shape: {self.bias.shape} (expected: [timesteps, batch_size + 1, embedding_dim])")
        savespace = torch.add(savespace, self.bias)
        #print(f"savespace after adding bias: {savespace.shape} (expected: [timesteps, batch_size + 1, embedding_dim])")

        # Pass savespace through each transformer block
        #print("Passing savespace through transformer blocks...")
        for idx, tfb in enumerate(self.tfb):
            #print(f"Passing through transformer block {idx + 1}...")
            savespace = tfb(x, savespace)
            #print(f"savespace after transformer block {idx + 1}: {savespace.shape} (expected: [timesteps, batch_size + 1, embedding_dim])")

        #print("====STM Forward Pass End ====")
        return savespace  # C x S x D - z5 in the paper


class TTM(nn.Module):  # Temporal transformer module
    def __init__(self, input, num_submatrices, num_blocks, num_heads, dtype):  # input -> # C x S x D
        super(TTM, self).__init__()
        self.dtype = dtype
        self.avgf = num_submatrices  # average factor (M)
        self.input = input.transpose(1, 3)  # D x S x C
        #print("+++++++++++++++",self.input.shape)
        self.seg = self.input.shape[1] / self.avgf



        if self.input.shape[1] % self.avgf != 0 or int(self.input.shape[1] / self.avgf) == 0:
            print(f"ERROR 3 - TTM : self.seg = {self.seg} != {self.input.shape[1]}/{self.avgf}")

        self.M_size1 = self.input.shape[2] * self.input.shape[3]
        self.tK = num_blocks  # number of transformer blocks - K in the paper
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)

        if self.M_size1 % self.hA != 0 or int(self.M_size1 / self.hA) == 0:  # - Dh = 121*(S+1) / num_heads
            print(f"ERROR 4 - TTM : self.Dh = {int(self.M_size1 / self.hA)} != {self.M_size1}/{self.hA} \nTry with different num_heads")

        self.weight = nn.Parameter(torch.randn(self.input.shape[0],self.M_size1, self.input.shape[2] * self.input.shape[3], dtype=self.dtype))
        self.bias = nn.Parameter(torch.zeros(self.inputshape[0], self.inputshape[3], self.inputshape[1] + 1, 510, dtype=self.dtype))
        self.cls = nn.Parameter(torch.zeros(self.inputshape[0], self.inputshape[3], 1, 510, dtype=self.dtype))

        trunc_normal(self.bias, std=.02)
        trunc_normal(self.cls, std=.02)
        self.tfb = nn.ModuleList([TemporalTFB(self.M_size1, self.hA, self.avgf, self.dtype) for _ in range(self.tK)])

        self.lnorm_extra = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # EXPERIMENTAL

    def forward(self, x):
        ##print("====TTM Forward Pass Start ====")
        #print(f"Input x shape before transpose: {x.shape}")  # Initial shape: [batch_size, channels, timesteps]
        
        input = x.transpose(1, 3)  # D x S x C
        #print(f"Input shape after transpose (D x S x C): {input.shape}")
        
        # Initialize inputc with zeros
        inputc = torch.zeros(input.shape[0], self.avgf, input.shape[2], input.shape[3], dtype=self.dtype).to(device)  # M x S x C
        #print(f"Initialized inputc (M x S x C): {inputc.shape}")
        
        # Perform segment-wise aggregation
        #print("Performing segment-wise aggregation for batch...")
        for b in range(input.shape[0]):  # Iterate over the batch dimension
            #print(f"Processing batch sample {b + 1}/{input.shape[0]}...")
            for i in range(self.avgf):  # Each segment covers input.shape[1]/avgf
                #print(f"Processing segment {i + 1}/{self.avgf} for batch {b + 1}...")
                for j in range(int(i * self.seg), int((i + 1) * self.seg)):  # Range for segment
                    #print(f"Adding input[{b}, {j}, :, :] to inputc[{b}, {i}, :, :]")
                    inputc[b, i, :, :] = inputc[b, i, :, :] + input[b, j, :, :]
                inputc[b, i, :, :] = inputc[b, i, :, :] / self.seg  # Average the segment
                #print(f"Segment {i + 1} after averaging for batch {b + 1}: inputc[{b}, {i}, :, :]")

        #print(f"inputc shape after aggregation: {inputc.shape}")
        
        # Reshape inputc
        altx = inputc.reshape(input.shape[0], self.avgf, input.shape[2] * input.shape[3]).to(device)  # M x L -> M x (S*C)
        #print(f"altx shape after reshape (M x (S*C)): {altx.shape}")
        
        # Initialize savespace
        savespace = torch.zeros(input.shape[0], self.avgf, self.M_size1, dtype=self.dtype).to(device)  # M x D

        savespace = torch.einsum('blm,bim -> bil', self.weight, altx.clone())

        savespace = torch.cat((self.cls, savespace), dim=1)  # Concatenate along the first dimension
        #print(f"savespace shape after concatenation (M+1 x D): {savespace.shape}")
        
        savespace = torch.add(savespace, self.bias)  # z -> M x D

        for idx, tfb in enumerate(self.tfb):
            #print(f"Transformer block {idx + 1}/{len(self.tfb)} input shape: {savespace.shape}")
            savespace = tfb(x, savespace)
            #print(f"Transformer block {idx + 1} output shape: {savespace.shape}")
        
        # Layer normalization
        #print("Applying final layer normalization...")
        savespace = self.lnorm_extra(savespace)
        #print(f"savespace shape after normalization: {savespace.shape}")
        
        # Reshape final output
        final_output = savespace.reshape(input.shape[0], self.avgf + 1, input.shape[2], input.shape[3])
        #print(f"Final output shape (avgf+1 x S x C): {final_output.shape}")
        
        #print("====TTM Forward Pass End ====")
        return final_output


class CNNdecoder(nn.Module):  # EEGformer decoder
    def __init__(self, input, num_cls, CF_second, dtype):  
        super(CNNdecoder, self).__init__()

        # Expecting input: [B, C, Scales, Time]
        self.b, self.c, self.s, self.m = input.shape  
        self.n = CF_second  # Number of outputs for cvd2
        self.dtype = dtype

        # **First Conv2D layer: Extract spatial features**
        self.cvd1 = nn.Conv2d(
            in_channels=self.c, 
            out_channels=32,  
            kernel_size=(3, 3), 
            padding=(1, 1), 
            stride=(1, 1), 
            dtype=self.dtype
        )

        # **Second Conv2D layer: Further refine features**
        self.cvd2 = nn.Conv2d(
            in_channels=32, 
            out_channels=64,  
            kernel_size=(3, 3), 
            padding=(1, 1), 
            stride=(1, 1), 
            dtype=self.dtype
        )

        # **Adaptive pooling to normalize different input sizes**
        self.pool = nn.AdaptiveAvgPool2d((self.n, int(self.m / 2)))

        # **Fully connected layer for classification**
        self.fc = nn.Linear(64 * self.n * int(self.m / 2), num_cls, dtype=self.dtype)

        # **Activation & Dropout for Regularization**
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(0.2)

    def forward(self, x):
        """
        Input: [batch_size, channels, scales, time] -> [B, C, S, M]
        """

        print(f"Decoder Input Shape: {x.shape}")  # Expecting [B, C, S, M]

        # **Apply first convolution**
        x = self.relu(self.cvd1(x))
        print(f"After Conv2D Layer 1: {x.shape}")  # Expected: [B, 32, S, M]

        # **Apply second convolution**
        x = self.relu(self.cvd2(x))
        print(f"After Conv2D Layer 2: {x.shape}")  # Expected: [B, 64, S, M]

        # **Apply adaptive pooling**
        x = self.pool(x)
        print(f"After Adaptive Pooling: {x.shape}")  # Expected: [B, 64, n, M/2]

        # **Flatten before fully connected layer**
        x = x.view(x.shape[0], -1)  # Flatten [B, 64, n, M/2] -> [B, 64 * n * (M/2)]
        print(f"After Flattening: {x.shape}")

        # **Pass through FC layer**
        x = self.fc(self.dropout(x))
        print(f"Final Output Shape: {x.shape}")  # Expected: [B, num_cls]

        return x



class EEGformer(nn.Module):
    def __init__(self, input, num_cls, input_channels, kernel_size, num_blocks, num_heads_RTM, CF_second, dtype=torch.float32):
    # def __init__(self, input, num_cls, input_channels, kernel_size, num_blocks, num_heads_RTM, num_heads_STM, num_heads_TTM, num_submatrices, CF_second, dtype=torch.float32):
        super(EEGformer, self).__init__()
        #print("input shape in model", input.shape)
        #print("input channels",input_channels)
        self.dtype = dtype
        self.ncf = 120
        self.num_cls = num_cls
        self.input_channels = input_channels
        self.kernel_size = kernel_size
        self.tK = num_blocks
        self.hA_rtm = num_heads_RTM
        # self.hA_stm = num_heads_STM
        # self.hA_ttm = num_heads_TTM
        # self.avgf = num_submatrices
        self.cfs = CF_second

        self.outshape1 = torch.zeros(input.shape[0], self.input_channels, self.ncf, input.shape[1] - 3 * (self.kernel_size - 1)).to(device)
        self.outshape2 = torch.zeros(self.outshape1.shape[0], self.outshape1.shape[1], self.outshape1.shape[2] + 1, self.outshape1.shape[3]).to(device)

        self.odcm = ODCM(input_channels, self.kernel_size, self.dtype)
        self.rtm = RTM(self.outshape1, self.tK, self.hA_rtm, self.dtype)
        # self.stm = STM(self.outshape2, self.tK, self.hA_stm, self.dtype)
        # self.ttm = TTM(self.outshape3, self.avgf, self.tK, self.hA_ttm, self.dtype)
        self.cnndecoder = CNNdecoder(self.outshape2, self.num_cls, self.cfs, self.dtype)
        self.fc_layer = torch.nn.Linear(120 * 150, num_cls)  # Adjust to match the desired flattened size and output classes
  
    def forward(self, x):
        print(f"Original input shape: {x.shape}")  # [batch, channels, time]

        # **Apply FFT on EEG data**
        # x = apply_fft(x)
        # print(f"FFT-applied input shape: {x.shape}")  # Shape should remain [batch, channels, time]
        
        # Apply Wavelet Transform
        x = apply_wavelet_transform(x)
        print(f"Wavelet Transform applied, input shape: {x.shape}")
      
        # Pass through CNN encoder
        x = self.odcm(x)
        print(f"Output shape from ODCM: {x.shape}")

        # Pass through RTM
        x = self.rtm(x)
        print(f"Output shape from RTM: {x.shape}")

        # Pass through STM
        # x = self.stm(x)
        # print(f"Output shape from LSTM: {x.shape}")

        # # Pass through TTM
        # x = self.ttm(x)
        # print(f"Output shape from TTM: {x.shape}")

        # CNN Decoder
        x = self.cnndecoder(x)
        print(f"Output shape from CNN Decoder: {x.shape}")

        #print(f"Output shape from CNN Decoder: {x.shape} (expected: [batch_size, num_classes])")

        # Softmax output
        #print("Before softmax", x.shape)
        output_softmax = torch.softmax(x, dim=-1).squeeze(1)

        #print(f"Output shape after softmax: {output_softmax.shape} (expected: [batch_size, num_classes])")
        #print("==== Forward Pass End ====")

        return output_softmax

    # CE - uses one hot encoded label or similar(such as multi class probability label)
    def eegloss(self, xf, label, L1_reg_const):  # CE Loss with L1 regularization
        wt = self.sa(self.cnndecoder.fc.weight) + self.sa(self.cnndecoder.cvd1.weight) + self.sa(self.cnndecoder.cvd2.weight) + self.sa(self.cnndecoder.cvd3.weight)
        wt += self.sa(self.ttm.mlp.fc1.weight) + self.sa(self.ttm.mlp.fc2.weight) + self.sa(self.ttm.lnorm.weight) + self.sa(self.ttm.lnormz.weight) + self.sa(self.ttm.weight)
        wt += self.sa(self.stm.mlp.fc1.weight) + self.sa(self.stm.mlp.fc2.weight) + self.sa(self.stm.lnorm.weight) + self.sa(self.stm.lnormz.weight) + self.sa(self.stm.weight)
        wt += self.sa(self.rtm.mlp.fc1.weight) + self.sa(self.rtm.mlp.fc2.weight) + self.sa(self.rtm.lnorm.weight) + self.sa(self.rtm.lnormz.weight) + self.sa(self.rtm.weight)
        wt += self.sa(self.odcm.cvf1.weight) + self.sa(self.odcm.cvf2.weight) + self.sa(self.odcm.cvf3.weight)

        for tfb in self.rtm.tfb:
            wt += self.sa(tfb.Wo) + self.sa(tfb.Wqkv)
        for tfb in self.stm.tfb:
            wt += self.sa(tfb.Wo) + self.sa(tfb.Wqkv)
        for tfb in self.ttm.tfb:
            wt += self.sa(tfb.Wo) + self.sa(tfb.Wqkv)

        ls = -(label * torch.log(xf) + (1 - label) * torch.log(1 - xf))
        ls = torch.mean(ls) + L1_reg_const * wt

        return ls

    def eegloss_light(self, xf, label, L1_reg_const):  # takes the weight sum of cnndecoder only
        wt = self.sa(self.cnndecoder.fc.weight) + self.sa(self.cnndecoder.cvd1.weight) + self.sa(self.cnndecoder.cvd2.weight) + self.sa(self.cnndecoder.cvd3.weight)
        ls = -(label * torch.log(xf) + (1 - label) * torch.log(1 - xf))
        ls = torch.mean(ls) + L1_reg_const * wt
        return ls

    def eegloss_wol1(self, xf, label):  # without L1
        ls = -(label * torch.log(xf) + (1 - label) * torch.log(1 - xf))
        ls = torch.mean(ls)
        return ls

    # BCE - does not need one hot encoding
    def bceloss(self, xf, label):  # BCE loss
        ls = -(label * torch.log(xf[:, 1]) + (1 - label) * torch.log(xf[:, 0]))
        ls = torch.mean(ls)
        return ls

    def bceloss_w(self, xf, label, numpos, numtot):  # Weighted BCE loss
        w0 = numtot / (2 * (numtot - numpos))
        w1 = numtot / (2 * numpos)
        ls = -(w1 * label * torch.log(xf[:, 1]) + w0 * (1 - label) * torch.log(xf[:, 0]))
        ls = torch.mean(ls)
        return ls

    def sa(self, t):
        return torch.sum(torch.abs(t))