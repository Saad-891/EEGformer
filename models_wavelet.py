import os
import torch
import torch.nn as nn
import math
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

class Mlp(nn.Module): # Multilayer perceptron
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0., dtype=torch.float32):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features, dtype=dtype)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features, dtype=dtype)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class GenericTFB(nn.Module):
    def __init__(self, emb_size, num_heads, dtype):
        super(GenericTFB, self).__init__()

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
        #print('Input x shape:', x.shape)  # Expected: [batch_size, channels, timesteps]
        #print('Input savespace shape:', savespace.shape)  # Expected: [batch_size, channels, timesteps, embedding_dim]

        # Initialize spaces with batch size included
        batch_size = x.shape[0]
        qkvspace = torch.zeros(
            batch_size, 3, x.shape[3], x.shape[1] + 1, self.hA, self.Dh, dtype=self.dtype
        ).to(device)  # Q, K, V
        atspace = torch.zeros(
            batch_size, x.shape[3], self.hA, x.shape[1] + 1, x.shape[1] + 1, dtype=self.dtype
        ).to(device)
        imv = torch.zeros(
            batch_size, x.shape[3], x.shape[1] + 1, self.hA, self.Dh, dtype=self.dtype
        ).to(device)

        #print('qkv space:', qkvspace.shape)
        #print('atspace:', atspace.shape)
        #print('imv:', imv.shape)

        # Compute Q, K, V using einsum with batch dimension
        qkvspace = torch.einsum('xhdm,bijm -> bxijhd', self.Wqkv, self.lnorm(savespace))
        #print('qkv space after einsum:', qkvspace.shape)  # [batch_size, 3, timesteps, channels+1, num_heads, Dh]

        # Compute attention scores
        atspace = (
            qkvspace[:, 0].clone().transpose(2, 3) / math.sqrt(self.Dh)
        ) @ qkvspace[:, 1].clone().transpose(2, 3).transpose(-2, -1)
        #print('atspace:', atspace.shape)  # [batch_size, timesteps, num_heads, channels+1, channels+1]

        # Compute intermediate vectors
        imv = (
            atspace.clone() @ qkvspace[:, 2].clone().transpose(2, 3)
        ).transpose(2, 3)
        #print('imv:', imv.shape)  # [batch_size, timesteps, channels+1, num_heads, Dh]

        # Compute new z (output)
        savespace = torch.einsum(
            'nm,bijn -> bijn', self.Wo, imv.clone().reshape(batch_size, x.shape[3], x.shape[1] + 1, self.M_size1)
        ) + savespace
        
        # savespace = torch.einsum('nm,ijm -> ijn', self.Wo, imv.clone().reshape(x.shape[2], x.shape[0] + 1, self.M_size1)) + savespace

        #print('savespace after einsum and addition:', savespace.shape)  # [batch_size, timesteps, channels+1, embedding_dim]

        # Normalize and pass through MLP
        savespace = self.mlp(self.lnormz(savespace)) + savespace
        #print('savespace after normalization and MLP:', savespace.shape)  # [batch_size, timesteps, channels+1, embedding_dim]

        return savespace


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
    def __init__(self, input_channels, kernel_size, dtype=torch.float32):
        super(ODCM, self).__init__()
        self.inpch = input_channels
        self.ksize = kernel_size  # 1X10
        self.ncf = 120  # The number of the depth-wise convolutional filter used in the three layers is set to 120
        self.dtype = dtype
        self.cvf1 = nn.Conv1d(in_channels=127, out_channels=self.inpch, kernel_size=self.ksize, padding='valid', stride=1, groups=self.inpch, dtype=self.dtype)
        self.cvf2 = nn.Conv1d(in_channels=self.cvf1.out_channels, out_channels=self.cvf1.out_channels, kernel_size=self.ksize, padding='valid', stride=1, groups=self.cvf1.out_channels, dtype=self.dtype)
        self.cvf3 = nn.Conv1d(in_channels=self.cvf2.out_channels, out_channels=self.ncf * self.cvf2.out_channels, kernel_size=self.ksize, padding='valid', stride=1, groups=self.cvf2.out_channels, dtype=self.dtype)
        self.relu = nn.ReLU()

  
    def forward(self, x):
        #print("====ODCM Forward Pass Start ====")
        #print(f"ODCM start. Input shape: {x.shape}")  # [batch_size, channels, time_steps]

        # Pass through first convolution
        x = self.cvf1(x)
        #print(f"After cvf1: {x.shape}")  # [batch_size, channels, reduced_time_steps]
        x = self.relu(x)

        # Pass through second convolution
        x = self.cvf2(x)
        #print(f"After cvf2: {x.shape}")  # [batch_size, channels, further_reduced_time_steps]
        x = self.relu(x)

        # Pass through third convolution
        x = self.cvf3(x)
        #print(f"After cvf3: {x.shape}")  # [batch_size, ncf, final_time_steps]
        x = self.relu(x)
        x = torch.reshape(x, (x.shape[0], (int)(x.shape[1] / self.ncf), self.ncf, (int)(x.shape[2])))
        #print(f"After reshape: {x.shape}")  # [batch_size, ncf, final_time_steps]
        

        #print("====ODCM Forward Pass END ====")
        return x


class RTM(nn.Module):  # Regional transformer module
    def __init__(self, input, num_blocks, num_heads, dtype):  # input -> S x C x D
        super(RTM, self).__init__()
        #print("Input shape RTM",input.shape)
        self.inputshape = input.transpose(1, 2).transpose(2, 3).shape  # C x D x S
        self.M_size1 = self.inputshape[2]  # -> D
        self.dtype = dtype
        #print("-----",self.inputshape)
        self.tK = num_blocks  # number of transformer blocks - K in the paper
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)  # Dh is the quotient computed by D/A and denotes the dimension number of three vectors.

        if self.M_size1 % self.hA != 0 or int(self.M_size1 / self.hA) == 0:
            print(f"ERROR 1 - RTM : self.Dh = {int(self.M_size1 / self.hA)} != {self.M_size1}/{self.hA} \nTry with different num_heads")

        self.weight = nn.Parameter(torch.randn(self.inputshape[0],self.M_size1, self.inputshape[2], dtype=self.dtype))
        self.bias = nn.Parameter(torch.zeros(self.inputshape[0],self.inputshape[3], self.inputshape[1] + 1, self.M_size1, dtype=self.dtype))  # S x C x D
        self.cls = nn.Parameter(torch.zeros(self.inputshape[0],self.inputshape[3], 1, self.M_size1, dtype=self.dtype))

        # self.cls = nn.Parameter(torch.zeros(self.inputshape[2], 1, self.M_size1, dtype=self.dtype))

        trunc_normal(self.bias, std=.02)
        trunc_normal(self.cls, std=.02)
        self.tfb = nn.ModuleList([GenericTFB(self.M_size1, self.hA, self.dtype) for _ in range(self.tK)])

    def forward(self, x):
        #print("====RTM Forward Pass Start ====")
        
        # Transpose the input tensor
        #print(f"Input shape before transpose: {x.shape}")  # [batch_size, channels, timesteps]
        x = x.transpose(1, 2).transpose(2, 3)  # Transpose to [channels, timesteps, batch_size]
        #print(f"Input shape after transpose (C x D x S): {x.shape}")  # C x D x S

        # Initialize savespace tensor
        #print(f"Initializing savespace with shape: [{x.shape[0]}, {x.shape[3]}, {x.shape[1]}, {self.M_size1}]")  # S x C x D
        savespace = torch.zeros(x.shape[0], x.shape[3], x.shape[1], self.M_size1, dtype=self.dtype).to(device)
        #print(f"savespace initialized with shape: {savespace.shape}")

        # Apply einsum operation
        #print("Performing einsum operation...")
        #print("Weight---------",self.weight.shape)
        #print("x---", x.shape)

        savespace = torch.einsum('bjk,bnki->binj', self.weight, x)
        # savespace = torch.einsum('lm,jmi -> ijl', self.weight, x)  # Matrix multiplication
        #print(f"savespace after einsum: {savespace.shape}")  # Expected: S x C x D

        # Concatenate class token
        #print(f"self.cls shape before concatenation: {self.cls.shape}")  # Expected: [timesteps, 1, embedding_dim]
        #print(f"savespace shape before concatenation: {savespace.shape}")  # Expected: [timesteps, channels, embedding_dim]
        savespace = torch.cat((self.cls, savespace), dim=2)  # Concatenate along channels (dim=1)
        #print(f"savespace shape after concatenation (with class token): {savespace.shape}")  # S x (C+1) x D

        # Add bias to savespace
        #print("Adding bias to savespace...")
        savespace = torch.add(savespace, self.bias)  # Element-wise addition
        #print(f"savespace shape after adding bias: {savespace.shape}")  # S x C x D

        # Pass through transformer blocks
        #print("Passing through transformer blocks...")
        for idx, tfb in enumerate(self.tfb):
         #   print(f"  Transformer block {idx + 1}: Input shape: {savespace.shape}")
            savespace = tfb(x, savespace)
          #  print(f"  Transformer block {idx + 1}: Output shape: {savespace.shape}")

        #print("====RTM Forward Pass End ====")
        return savespace  # Final shape: S x C x D


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
        self.bias = nn.Parameter(torch.zeros(self.input.shape[0], self.avgf + 1, self.M_size1, dtype=self.dtype))
        self.cls = nn.Parameter(torch.zeros(self.input.shape[0], 1, self.M_size1, dtype=self.dtype))
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
        #print(f"Initialized savespace (M x D): {savespace.shape}")
        
        # Perform einsum operation
        #print("Performing einsum operation...")
        #print(f"Weight shape: {self.weight.shape}")  # lm
        #print(f"altx shape: {altx.shape}")  # im
        savespace = torch.einsum('blm,bim -> bil', self.weight, altx.clone())
        #print(f"savespace after einsum (M x D): {savespace.shape}")
        
        # Concatenate class token
        #print("Concatenating class token...")
        #print(f"Class token shape: {self.cls.shape}")
        #print(f"savespace shape before concatenation: {savespace.shape}")
        savespace = torch.cat((self.cls, savespace), dim=1)  # Concatenate along the first dimension
        #print(f"savespace shape after concatenation (M+1 x D): {savespace.shape}")
        
        # Add bias to savespace
        #print("Adding bias to savespace...")
        #print(f"Bias shape: {self.bias.shape}")
        savespace = torch.add(savespace, self.bias)  # z -> M x D
        #print(f"savespace shape after adding bias: {savespace.shape}")
        
        # Pass through transformer blocks
        #print("Passing savespace through transformer blocks...")
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
    def __init__(self, input, num_cls, CF_second, dtype):  # input -> # M x S x C
        super(CNNdecoder, self).__init__()
        self.input = input.transpose(1, 2).transpose(2, 3)  # S x C x M
        self.b = self.input.shape[0]  # B: Batch size
        self.s = self.input.shape[1]  # S: Sequence length
        self.c = self.input.shape[2]  # C: Channels
        self.m = self.input.shape[3]  # M: Feature dimension
        self.n = CF_second  # Number of outputs for cvd2

        self.dtype = dtype

        # First convolution: Handles the `C` dimension
        self.cvd1 = nn.Conv1d(in_channels=self.c, out_channels=1, kernel_size=1, dtype=self.dtype)  # Input: [B, C, M]

        # Second convolution: Handles the `S` dimension
        self.cvd2 = nn.Conv1d(in_channels=self.s, out_channels=self.n, kernel_size=1, dtype=self.dtype)  # Input: [B, S, M]

        # Third convolution: Handles the `M` dimension
        self.cvd3 = nn.Conv1d(in_channels=self.m, out_channels=int(self.m / 2), kernel_size=1, dtype=self.dtype)  # Input: [B, M, N]

        # Fully connected layer
        self.fc = nn.Linear(int(self.m / 2) * self.n, num_cls, dtype=self.dtype)  # Input: [B, (M/2)*N]

        # Activation
        self.relu = nn.ReLU()


    def forward(self, x):  # x -> [B, M, S, C]
        #print("==== CNN Decoder Forward Pass Start ====")
        #print(f"Input shape (B x M x S x C): {x.shape}")

        # Extract dimensions
        B, M, S, C = x.shape

        # Initialize an empty list to store outputs for each batch
        batch_outputs = []

        # Loop over each batch
        for b in range(B):
            #print(f"Processing batch {b + 1}/{B}")

            # Extract the batch slice
            x_batch = x[b]  # Shape: [M, S, C]
            #print(f"Batch {b} input shape: {x_batch.shape}")

            # Transpose to expected Conv1D input: [M, C, S]
            x_batch = x_batch.transpose(0, 1).transpose(1, 2)  # S x C x M
      
            #print(f"Batch {b} after transpose (M x C x S): {x_batch.shape}")

            # Apply the first convolution
            x_batch = self.cvd1(x_batch)  # [M, 1, S]
            #print(f"Batch {b} after first convolution (cvd1), shape: {x_batch.shape}")
            x_batch = self.relu(x_batch)
            #print(f"Batch {b} after ReLU activation (cvd1), shape: {x_batch.shape}")

            # Remove the singleton channel dimension
            x_batch = x_batch[:, 0, :]  # [M, S]
            #print(f"Batch {b} after squeezing channel dimension, shape: {x_batch.shape}")

            # Apply the second convolution
            x_batch = self.cvd2(x_batch).transpose(0,1)  # [M, N]
            #print(f"Batch {b} after second convolution (cvd2), shape: {x_batch.shape}")
            x_batch = self.relu(x_batch)
            #print(f"Batch {b} after ReLU activation (cvd2), shape: {x_batch.shape}")

            # Apply the third convolution
            x_batch = self.cvd3(x_batch)  # [M, M/2]
            #print(f"Batch {b} after third convolution (cvd3), shape: {x_batch.shape}")
            x_batch = self.relu(x_batch)
            #print(f"Batch {b} after ReLU activation (cvd3), shape: {x_batch.shape}")

            # Flatten the output and pass through the FC layer
            x_batch_flattened_shape = x_batch.shape[0] * x_batch.shape[1] # [M, Flattened]
            #print(f"Batch {b} flattened for FC layer: {x_batch_flattened_shape}")
            x_batch_output = self.fc(x_batch.reshape(1,x_batch_flattened_shape))  # [M, num_cls]
            #print(f"Batch {b} after FC layer, shape: {x_batch_output.shape}")

            # Append batch output to list
            batch_outputs.append(x_batch_output)

        # Concatenate all batch outputs into a single tensor
        output = torch.stack(batch_outputs, dim=0)  # Shape: [B, M, num_cls]
        #print(f"Final output shape (B x M x num_cls): {output.shape}")
        #print("==== CNN Decoder Forward Pass End ====")
        return output



class EEGformer(nn.Module):
    def __init__(self, input, num_cls, input_channels, kernel_size, num_blocks, num_heads_RTM, num_heads_STM, num_heads_TTM, num_submatrices, CF_second, dtype=torch.float32):
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
        self.hA_stm = num_heads_STM
        self.hA_ttm = num_heads_TTM
        self.avgf = num_submatrices
        self.cfs = CF_second

        self.outshape1 = torch.zeros(input.shape[0], self.input_channels, self.ncf, input.shape[1] - 3 * (self.kernel_size - 1)).to(device)
        #old self.outshape1 = torch.zeros(self.input_channels, self.ncf, input.shape[0] - 3 * (self.kernel_size - 1)).to(device)
        self.outshape2 = torch.zeros(self.outshape1.shape[0], self.outshape1.shape[1], self.outshape1.shape[2] + 1, self.outshape1.shape[3]).to(device)
        #old self.outshape2 = torch.zeros(self.outshape1.shape[0], self.outshape1.shape[1] + 1, self.outshape1.shape[2]).to(device)
        self.outshape3 = torch.zeros(self.outshape2.shape[0], self.outshape2.shape[2], self.outshape2.shape[1] + 1, self.outshape2.shape[3]).to(device)
        #old self.outshape3 = torch.zeros(self.outshape2.shape[1], self.outshape2.shape[0] + 1, self.outshape2.shape[2]).to(device)
        self.outshape4 = torch.zeros(self.outshape3.shape[0], self.avgf + 1, self.outshape3.shape[2], self.outshape3.shape[1]).to(device)
        #old self.outshape4 = torch.zeros(self.avgf + 1, self.outshape3.shape[1], self.outshape3.shape[0]).to(device)

        self.odcm = ODCM(input_channels, self.kernel_size, self.dtype)
        self.rtm = RTM(self.outshape1, self.tK, self.hA_rtm, self.dtype)
        self.stm = STM(self.outshape2, self.tK, self.hA_stm, self.dtype)
        self.ttm = TTM(self.outshape3, self.avgf, self.tK, self.hA_ttm, self.dtype)
        self.cnndecoder = CNNdecoder(self.outshape4, self.num_cls, self.cfs, self.dtype)
        
        self.fc_layer = torch.nn.Linear(120 * 150, num_cls)  # Adjust to match the desired flattened size and output classes

class EEGformer(nn.Module):
    def __init__(self, input, num_cls, input_channels, kernel_size, num_blocks, num_heads_RTM, num_heads_STM, num_heads_TTM, num_submatrices, CF_second, dtype=torch.float32):
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
        self.hA_stm = num_heads_STM
        self.hA_ttm = num_heads_TTM
        self.avgf = num_submatrices
        self.cfs = CF_second

        self.outshape1 = torch.zeros(input.shape[0], self.input_channels, self.ncf, input.shape[1] - 3 * (self.kernel_size - 1)).to(device)
        #old self.outshape1 = torch.zeros(self.input_channels, self.ncf, input.shape[0] - 3 * (self.kernel_size - 1)).to(device)
        self.outshape2 = torch.zeros(self.outshape1.shape[0], self.outshape1.shape[1], self.outshape1.shape[2] + 1, self.outshape1.shape[3]).to(device)
        #old self.outshape2 = torch.zeros(self.outshape1.shape[0], self.outshape1.shape[1] + 1, self.outshape1.shape[2]).to(device)
        self.outshape3 = torch.zeros(self.outshape2.shape[0], self.outshape2.shape[2], self.outshape2.shape[1] + 1, self.outshape2.shape[3]).to(device)
        #old self.outshape3 = torch.zeros(self.outshape2.shape[1], self.outshape2.shape[0] + 1, self.outshape2.shape[2]).to(device)
        self.outshape4 = torch.zeros(self.outshape3.shape[0], self.avgf + 1, self.outshape3.shape[2], self.outshape3.shape[1]).to(device)
        #old self.outshape4 = torch.zeros(self.avgf + 1, self.outshape3.shape[1], self.outshape3.shape[0]).to(device)

        self.odcm = ODCM(input_channels, self.kernel_size, self.dtype)
        self.rtm = RTM(self.outshape1, self.tK, self.hA_rtm, self.dtype)
        self.stm = STM(self.outshape2, self.tK, self.hA_stm, self.dtype)
        self.ttm = TTM(self.outshape3, self.avgf, self.tK, self.hA_ttm, self.dtype)
        self.cnndecoder = CNNdecoder(self.outshape4, self.num_cls, self.cfs, self.dtype)
        
        self.fc_layer = torch.nn.Linear(120 * 150, num_cls)  # Adjust to match the desired flattened size and output classes

    def forward(self, x):
        #print(f"Shape of input{x.shape}")
        x = apply_wavelet_transform(x)
        #print(f"shape post wavelet transform{x.shape}")

        x = x.reshape(x.shape[0], -1, x.shape[-1])  # Reshape to [B, C * S, T]
        #print(f'reshaped wavelet {x.shape}')

        #print("==== Forward Pass Start ====")
        # x = self.odcm(x.transpose(1, 2))
        #print(f"Input shape to ODCM: {x.shape} (expected: [batch_size, channels, timesteps])")
        x = self.odcm(x)
        #print(f"Output shape from ODCM: {x.shape} (expected: [batch_size, ncf, reduced_timesteps])")

        # Pass through RTM
        #print(f"Input shape to RTM: {x.shape} (expected: [batch_size, channels, reduced_timesteps])")
        x = self.rtm(x)
        #print(f"Output shape from RTM: {x.shape} (expected: [batch_size, timesteps, channels, embedding_dim])")

        # Pass through STM
        #print(f"Input shape to STM: {x.shape} (expected: [batch_size, timesteps, channels, embedding_dim])")
        x = self.stm(x)
        #print(f"Output shape from STM: {x.shape} (expected: [batch_size, reduced_timesteps, embedding_dim])")

        # Pass through TTM
        #print(f"Input shape to TTM: {x.shape} (expected: [batch_size, reduced_timesteps, embedding_dim])")
        x = self.ttm(x)
        #print(f"Output shape from TTM: {x.shape} (expected: [batch_size, submatrices, embedding_dim])")

        # CNN Decoder
        #print(f"Input shape to CNN Decoder: {x.shape} (expected: [batch_size, submatrices, embedding_dim])")
        x = self.cnndecoder(x)
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