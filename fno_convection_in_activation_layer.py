# -*- coding: utf-8 -*-
"""fno convection in activation layer.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1QQlNNT5jb1VmiKggcQ7Q_O-x0u6eIzac
"""

!git clone -b master  https://github.com/neuraloperator/neuraloperator.git

from google.colab import drive
drive.mount('/content/drive')



import torch

def burgers_upwind_scheme_1d_batch(u, dx, dt, nu):
    """
    Apply one-dimensional upwind scheme for Burger's equation in batch mode.

    Parameters:
    u (torch.Tensor): The initial state of the velocity field (batched).
    dx (float): Spatial step size.
    dt (float): Time step size.
    nu (float): Viscosity coefficient.

    Returns:
    torch.Tensor: The updated state of the field after one time step for each batch.
    """
    u_new = u.clone()

    # Iterate over each element in the batch
    for b in range(u.shape[0]):
        # Compute the advection and diffusion terms using upwind scheme
        for i in range(1, u.shape[1] - 1):
            if u[b, i, 0] > 0:
                # Upwind scheme for positive velocity
                adv_term = u[b, i, 0] * (u[b, i, 0] - u[b, i - 1, 0]) / dx
            else:
                # Upwind scheme for negative velocity
                adv_term = u[b, i, 0] * (u[b, i + 1, 0] - u[b, i, 0]) / dx

            # Compute the diffusion term
            diff_term = nu * (u[b, i + 1, 0] - 2 * u[b, i, 0] + u[b, i - 1, 0]) / dx**2

            # Update the field
            u_new[b, i, 0] = u[b, i, 0] - dt * adv_term + dt * diff_term

    return u_new

"""
@author: Zongyi Li
This file is the Fourier Neural Operator for 1D problem such as the (time-independent) Burgers equation discussed in Section 5.1 in the [paper](https://arxiv.org/pdf/2010.08895.pdf).
"""

import torch.nn.functional as F
from timeit import default_timer
from utilities3 import *
import matplotlib.pyplot as plt
torch.manual_seed(3407)
np.random.seed(0)

#complex relu
def complex_relu_real_imag(z):
    real = torch.relu(z.real)
    imag = torch.relu(z.imag)
    return torch.complex(real, imag)


################################################################
#  1d fourier layer
################################################################
class SpectralConv1d(nn.Module):
    def __init__(self, in_channels, out_channels, modes_diffusion, modes_convection):
        super(SpectralConv1d, self).__init__()

        """
        1D Fourier layer. It does FFT, linear transform, and Inverse FFT.
        """

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.modes_diffusion = modes_diffusion  # Modes for diffusion
        self.modes_convection = modes_convection  # Modes for convection

        self.scale = (1 / (in_channels * out_channels))
        self.diffusion_weights = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes_diffusion, dtype=torch.cfloat))
        self.convection_weights = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes_convection, dtype=torch.cfloat))

    # Complex multiplication
    def compl_mul1d(self, input, weights):
        # (batch, in_channel, x), (in_channel, out_channel, x) -> (batch, out_channel, x)
        return torch.einsum("bix,iox->box", input, weights)

    def forward(self, x):
        batchsize = x.shape[0]
        # Compute Fourier coefficients up to a factor of e^(- something constant)
        x_ft = torch.fft.rfft(x)

        # Initialize output Fourier components
        diffusion_out_ft = torch.zeros(batchsize, self.out_channels, x.size(-1) // 2 + 1, device=x.device, dtype=torch.cfloat)
        convection_out_ft = torch.zeros(batchsize, self.out_channels, x.size(-1) // 2 + 1, device=x.device, dtype=torch.cfloat)

        # Perform complex multiplication for diffusion using specified modes
        diffusion_out_ft[:, :, :self.modes_diffusion] = self.compl_mul1d(x_ft[:, :, :self.modes_diffusion], self.diffusion_weights)

        # Perform complex multiplication for convection using specified modes (higher-frequency modes)
        convection_out_ft[:, :, :self.modes_convection] = self.compl_mul1d(x_ft[:, :, -self.modes_convection:], self.convection_weights)

        #convection_out_ft = torch.tanh(convection_out_ft)

        #complex relu activating
        convection_out_ft = complex_relu_real_imag(convection_out_ft)

        # Combine diffusion and convection in the Fourier domain
        #total_out_ft = diffusion_out_ft + convection_out_ft
        total_out_ft = diffusion_out_ft

        # Return to physical space
        x = torch.fft.irfft(total_out_ft, n=x.size(-1))
        convection_out_ft = torch.fft.irfft(convection_out_ft, n=x.size(-1))
        return x, convection_out_ft



class MLP(nn.Module):
    def __init__(self, in_channels, out_channels, mid_channels):
        super(MLP, self).__init__()
        self.mlp1 = nn.Conv1d(in_channels, mid_channels, 1)
        self.mlp2 = nn.Conv1d(mid_channels, out_channels, 1)

    def forward(self, x):
        x = self.mlp1(x)
        x = F.gelu(x)
        x = self.mlp2(x)
        return x

class FNO1d(nn.Module):
    def __init__(self, modes_diffusion, modes_convection, width):
        super(FNO1d, self).__init__()

        """
        The overall network. It contains 4 layers of the Fourier layer.
        1. Lift the input to the desire channel dimension by self.fc0 .
        2. 4 layers of the integral operators u' = (W + K)(u).
            W defined by self.w; K defined by self.conv .
        3. Project from the channel space to the output space by self.fc1 and self.fc2 .

        input: the solution of the initial condition and location (a(x), x)
        input shape: (batchsize, x=s, c=2)
        output: the solution of a later timestep
        output shape: (batchsize, x=s, c=1)
        """

        self.modes_diffusion = modes_diffusion
        self.modes_convection= modes_convection
        self.width = width
        self.padding = 8 # pad the domain if input is non-periodic

        self.p = nn.Linear(2, self.width) # input channel_dim is 2: (u0(x), x)
        self.conv0 = SpectralConv1d(self.width, self.width, self.modes_diffusion, self.modes_convection)
        self.conv1 = SpectralConv1d(self.width, self.width, self.modes_diffusion, self.modes_convection)
        self.conv2 = SpectralConv1d(self.width, self.width, self.modes_diffusion, self.modes_convection)
        self.conv3 = SpectralConv1d(self.width, self.width, self.modes_diffusion, self.modes_convection)
        self.mlp0 = MLP(self.width, self.width, self.width)
        self.mlp1 = MLP(self.width, self.width, self.width)
        self.mlp2 = MLP(self.width, self.width, self.width)
        self.mlp3 = MLP(self.width, self.width, self.width)
        self.w0 = nn.Conv1d(self.width, self.width, 1)
        self.w1 = nn.Conv1d(self.width, self.width, 1)
        self.w2 = nn.Conv1d(self.width, self.width, 1)
        self.w3 = nn.Conv1d(self.width, self.width, 1)
        self.q = MLP(self.width, 1, self.width*2)  # output channel_dim is 1: u1(x)

    def forward(self, x):
        grid = self.get_grid(x.shape, x.device)
        #print(x.shape)  # Should be something like [batch_size, features]
        #print(grid.shape)  # Should also be [batch_size, features] or compatible for concatenation

        x = torch.cat((x, grid), dim=-1)
        x = self.p(x)
        x = x.permute(0, 2, 1)
        # x = F.pad(x, [0,self.padding]) # pad the domain if input is non-periodic

        x1, convec1 = self.conv0(x)
        x1 = self.mlp0(x1)
        x2 = self.w0(x + convec1)
        x = x1 + x2
        #integrate the nonlinear convection part into the nonlinear activating function
        x = F.gelu(x+convec1)
        #x = F.dropout(x,0.5)

        x1,convec2 = self.conv1(x)
        x1 = self.mlp1(x1)
        x2 = self.w1(x + convec2)
        x = x1 + x2
        x = F.gelu(x+convec2)
        #x = F.dropout(x,0.5)

        x1,convec3 = self.conv2(x)
        x1 = self.mlp2(x1)
        x2 = self.w2(x + convec3)
        x = x1 + x2
        x = F.gelu(x+convec3)
        #x = F.dropout(x,0.5)



        x1,convec4 = self.conv3(x)
        x1 = self.mlp3(x1)
        x2 = self.w3(x+convec4)
        x = x1 + x2

        # x = x[..., :-self.padding] # pad the domain if input is non-periodic
        x = self.q(x)
        x = x.permute(0, 2, 1)
        return x

    def get_grid(self, shape, device):
        batchsize, size_x = shape[0], shape[1]
        gridx = torch.tensor(np.linspace(0, 1, size_x), dtype=torch.float)
        gridx = gridx.reshape(1, size_x, 1).repeat([batchsize, 1, 1])
        return gridx.to(device)

################################################################
#  configurations
################################################################
ntrain = 200
ntest = 20

sub = 1 #subsampling rate
h = 2**13 // sub #total grid size divided by the subsampling rate
s = h

batch_size = 20
learning_rate = 0.001
epochs = 700
iterations = epochs*(ntrain//batch_size)

modes_convection=8
modes_diffusion=16
width = 64

################################################################
# read data
################################################################

# Data is of the shape (number of samples, grid size)
dataloader = MatReader('/content/drive/My Drive/burgers/BurgersData11.mat')
x_data = dataloader.read_field('input')[:,::sub]
y_data = dataloader.read_field('output')[:,::sub]

x_train = x_data[:ntrain,:]
y_train = y_data[:ntrain,:]
x_test = x_data[-ntest:,:]
y_test = y_data[-ntest:,:]

x_train = x_train.reshape(ntrain,s,1)
x_test = x_test.reshape(ntest,s,1)

train_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(x_train, y_train), batch_size=batch_size, shuffle=True)
test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(x_test, y_test), batch_size=batch_size, shuffle=False)

# model
model = FNO1d(modes_diffusion, modes_convection, width).cuda()
print(count_params(model))

################################################################
# training and evaluation
################################################################
optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=iterations)

myloss = LpLoss(size_average=False)
for ep in range(epochs):
    model.train()
    t1 = default_timer()
    train_mse = 0
    train_l2 = 0
    for x, y in train_loader:

        x, y = x.cuda(), y.cuda()
        # print(y.shape)
        #x = x.reshape(-1)
        #x = burgers_upwind_scheme_1d(x, dx = 2/len(x), dt = 0.001, nu = 0.0001)
        #x = x.reshape(20,-1,1)

        optimizer.zero_grad()
        out = model(x)

        mse = F.mse_loss(out.view(batch_size, -1), y.view(batch_size, -1), reduction='mean')
        l2 = myloss(out.view(batch_size, -1), y.view(batch_size, -1))
        l2.backward() # use the l2 relative loss

        optimizer.step()
        scheduler.step()
        train_mse += mse.item()
        train_l2 += l2.item()

    model.eval()
    test_l2 = 0.0
    with torch.no_grad():
        for x, y in test_loader:
            x, y = x.cuda(), y.cuda()

            out = model(x)
            test_l2 += myloss(out.view(batch_size, -1), y.view(batch_size, -1)).item()

    train_mse /= len(train_loader)
    train_l2 /= ntrain
    test_l2 /= ntest

    t2 = default_timer()
    print(ep, t2-t1, train_mse, train_l2, test_l2)

# Select a specific sample from the test set (e.g., the first sample)
sample_index = 10  # Change this index to select a different sample
x, y = test_loader.dataset[sample_index]
print(x.shape)
print(y.shape)
    # Move to CUDA if using GPU
x, y = x.cuda(), y.cuda()
print(x.shape)
print(y.shape)
##x = x.reshape(-1)
#x = burgers_upwind_scheme_1d(x, dx = 2/len(x), dt = 0.001, nu = 0.0001)
#x = x.reshape(20,-1,1)
# Use the model to predict the output
with torch.no_grad():
  pred = model(x.unsqueeze(0)).view(-1)

    # Convert tensors to CPU for plotting
x = x.squeeze().cpu()
y = y.squeeze().cpu()
pred = pred.cpu()

    # Plotting
plt.figure(figsize=(12, 8))

    # Plot initial condition
plt.subplot(3, 1, 1)
plt.plot(x, label='Initial Condition')
plt.title('Initial Condition')
plt.legend()

 # Plot corresponding output data
plt.subplot(3, 1, 2)
plt.plot(y, label='True Output')
plt.title('True Output Data')
plt.legend()


    # Plot model's prediction
plt.subplot(3, 1, 3)
plt.plot(pred, label='Model Prediction')
plt.title('Model Prediction')
plt.legend()

plt.tight_layout()
plt.savefig('/content/drive/My Drive/plot26.png')