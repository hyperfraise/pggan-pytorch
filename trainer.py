import dataloader as DL
from config import config
import network as net
from math import floor, ceil
import os, sys
import torch
import torchvision.transforms as transforms
from torch.autograd import Variable
from torch.optim import Adam
from tqdm import tqdm
import tf_recorder as tensorboard
import utils as utils
import numpy as np
from multiprocessing import Manager, Value
from torch.autograd import grad as torch_grad

# import tensorflow as tf
def safe_reading(file):
    value = file.read()
    try:
        value = int(value)
        return value
    except:
        return 0


def accelerate(value):
    return value * 2


class trainer:
    def __init__(self, config):
        self.config = config
        if torch.cuda.is_available():
            self.use_cuda = True
            torch.set_default_tensor_type("torch.cuda.FloatTensor")
        else:
            self.use_cuda = False
            torch.set_default_tensor_type("torch.FloatTensor")

        self.nz = config.nz
        self.optimizer = config.optimizer

        self.resl = 2  # we start from 2^2 = 4
        self.lr = config.lr
        self.eps_drift = config.eps_drift
        self.smoothing = config.smoothing
        self.max_resl = config.max_resl
        self.accelerate = 1
        self.wgan_target = 1.0
        self.trns_tick = config.trns_tick
        self.stab_tick = config.stab_tick
        self.TICK = config.TICK
        self.skip = False
        self.globalIter = 0
        self.globalTick = 0
        self.wgan_epsilon = 0.001
        self.stack = 0
        self.wgan_lambda = 10.0
        self.just_passed = False
        if self.config.resume:
            saved_models = os.listdir("repo/model/")
            iterations = list(
                map(lambda x: int(x.split("_")[-1].split(".")[0][1:]), saved_models)
            )
            self.last_iteration = max(iterations)
            selected_indexes = np.where([x == self.last_iteration for x in iterations])[
                0
            ]
            G_last_model = [
                saved_models[x] for x in selected_indexes if "gen" in saved_models[x]
            ][0]
            D_last_model = [
                saved_models[x] for x in selected_indexes if "dis" in saved_models[x]
            ][0]
            saved_grids = os.listdir("repo/save/grid")
            global_iterations = list(map(lambda x: int(x.split("_")[0]), saved_grids))
            self.globalIter = self.config.save_img_every * max(global_iterations)
            print(
                "Resuming after "
                + str(self.last_iteration)
                + " ticks and "
                + str(self.globalIter)
                + " iterations"
            )
            G_weights = torch.load("repo/model/" + G_last_model)
            D_weights = torch.load("repo/model/" + D_last_model)
            self.resuming = True
        else:
            self.resuming = False

        self.kimgs = 0
        self.stack = 0
        self.epoch = 0
        self.fadein = {"gen": None, "dis": None}
        self.complete = {"gen": 0, "dis": 0}
        self.phase = "init"
        self.flag_flush_gen = False
        self.flag_flush_dis = False
        self.flag_add_noise = self.config.flag_add_noise
        self.flag_add_drift = self.config.flag_add_drift

        # network and cirterion
        self.G = net.Generator(config)
        self.D = net.Discriminator(config)
        print("Generator structure: ")
        print(self.G.model)
        print("Discriminator structure: ")
        print(self.D.model)
        self.mse = torch.nn.MSELoss()
        if self.use_cuda:
            self.mse = self.mse.cuda()
            torch.cuda.manual_seed(config.random_seed)
            self.G = torch.nn.DataParallel(self.G, device_ids=[0]).cuda(device=0)
            self.D = torch.nn.DataParallel(self.D, device_ids=[0]).cuda(device=0)

        # define tensors, ship model to cuda, and get dataloader.
        self.renew_everything()
        if self.resuming:
            while self.globalTick != self.last_iteration:
                self.resl_scheduler()
            while ((self.kimgs + self.batchsize) % self.TICK) >= (
                self.kimgs % self.TICK
            ):
                self.resl_scheduler()
            self.epoch = int(self.last_iteration * 1000 / len(self.loader.dataset))

            print(
                "Resuming at "
                + str(self.resl)
                + " definition after "
                + str(self.epoch)
                + " epochs"
            )
            self.G.module.load_state_dict(G_weights["state_dict"])
            self.D.module.load_state_dict(D_weights["state_dict"])
            self.opt_g.load_state_dict(G_weights["optimizer"])
            self.opt_d.load_state_dict(D_weights["optimizer"])

        # tensorboard
        self.use_tb = config.use_tb
        if self.use_tb:
            self.tb = tensorboard.tf_recorder()

    def resl_scheduler(self):
        """
        this function will schedule image resolution(self.resl) progressively.
        it should be called every iteration to ensure resl value is updated properly.
        step 1. (trns_tick) --> transition in generator.
        step 2. (stab_tick) --> stabilize.
        step 3. (trns_tick) --> transition in discriminator.
        step 4. (stab_tick) --> stabilize.
        """

        self.previous_phase = self.phase
        if self.phase[1:] != "trns":
            self.accelerate = 1

        if floor(self.resl) != 2:
            self.trns_tick = self.config.trns_tick
            self.stab_tick = self.config.stab_tick

        self.batchsize = self.loader.batchsize
        delta = 1.0 / (2 * self.trns_tick + 2 * self.stab_tick)
        d_alpha = 1.0 * self.batchsize / self.trns_tick / self.TICK

        # update alpha if fade-in layer exist.
        if self.fadein["gen"] is not None:
            if self.resl % 1.0 < (self.trns_tick) * delta:
                self.fadein["gen"].update_alpha(d_alpha)
                self.complete["gen"] = self.fadein["gen"].alpha * 100
                self.phase = "gtrns"
            elif (
                self.resl % 1.0 >= (self.trns_tick) * delta
                and self.resl % 1.0 < (self.trns_tick + self.stab_tick) * delta
            ):
                self.phase = "gstab"
        if self.fadein["dis"] is not None:
            if (
                self.resl % 1.0 >= (self.trns_tick + self.stab_tick) * delta
                and self.resl % 1.0 < (self.stab_tick + self.trns_tick * 2) * delta
            ):
                self.fadein["dis"].update_alpha(d_alpha)
                self.complete["dis"] = self.fadein["dis"].alpha * 100
                self.phase = "dtrns"
            elif (
                self.resl % 1.0 >= (self.stab_tick + self.trns_tick * 2) * delta
                and self.phase != "final"
            ):
                self.phase = "dstab"

        prev_kimgs = self.kimgs
        self.kimgs = self.kimgs + self.batchsize
        if (self.kimgs % self.TICK) < (prev_kimgs % self.TICK):
            self.globalTick = self.globalTick + 1
            if self.resuming and self.globalTick > self.last_iteration:
                self.resuming = False
            # increase linearly every tick, and grow network structure.
            prev_resl = floor(self.resl)
            f = open("continue.txt", "r")
            if safe_reading(f):
                f.close()
                if self.phase[1:] == "trns":
                    self.accelerate = accelerate(self.accelerate)
                else:
                    self.skip = True
                f = open("continue.txt", "w")
                f.write("0")
            self.resl = self.resl + delta
            f.close()
            self.resl = max(2, min(10.5, self.resl))  # clamping, range: 4 ~ 1024
            # flush network.
            if (
                self.flag_flush_gen
                and self.resl % 1.0 >= (self.trns_tick + self.stab_tick) * delta
                and prev_resl != 2
            ):
                if self.fadein["gen"] is not None:
                    self.fadein["gen"].update_alpha(d_alpha)
                    self.complete["gen"] = self.fadein["gen"].alpha * 100
                self.flag_flush_gen = False
                self.G.module.flush_network()  # flush G
                # print(self.G.module.model)
                # self.Gs.module.flush_network()         # flush Gs
                self.fadein["gen"] = None
                self.complete["gen"] = 0.0
                self.phase = "dtrns"
                print("flush gen, stop fadein gen, begin phase " + self.phase)
                self.just_passed = True
            elif (
                self.flag_flush_dis and floor(self.resl) != prev_resl and prev_resl != 2
            ):
                if self.fadein["dis"] is not None:
                    self.fadein["dis"].update_alpha(d_alpha)
                    self.complete["dis"] = self.fadein["dis"].alpha * 100
                self.flag_flush_dis = False
                self.D.module.flush_network()  # flush and,
                # print(self.D.module.model)
                self.fadein["dis"] = None
                self.complete["dis"] = 0.0
                if floor(self.resl) < self.max_resl and self.phase != "final":
                    self.phase = "gtrns"
                print("flush dis, stop fadein dis, begin phase " + self.phase)
                self.just_passed = True

            # grow network.
            if floor(self.resl) != prev_resl and floor(self.resl) < self.max_resl + 1:
                self.lr = self.lr * float(self.config.lr_decay)
                self.G.module.grow_network(floor(self.resl))
                # self.Gs.grow_network(floor(self.resl))
                self.D.module.grow_network(floor(self.resl))
                self.renew_everything()
                self.fadein["gen"] = dict(self.G.module.model.named_children())[
                    "fadein_block"
                ]
                self.fadein["dis"] = dict(self.D.module.model.named_children())[
                    "fadein_block"
                ]
                self.flag_flush_gen = True
                self.flag_flush_dis = True
                self.just_passed = True
                print("grow network, begin fadein phases")

            if (
                floor(self.resl) >= self.max_resl
                and self.resl % 1.0 >= (self.stab_tick + self.trns_tick * 2) * delta
            ):
                self.phase = "final"
                self.resl = (
                    self.max_resl + (self.stab_tick + self.trns_tick * 2) * delta
                )

    def renew_everything(self):
        # renew dataloader.
        self.loader = DL.dataloader(config)
        self.loader.renew(min(floor(self.resl), self.max_resl))

        # define tensors
        self.z = torch.FloatTensor(self.loader.batchsize, self.nz)
        self.x = torch.FloatTensor(
            self.loader.batchsize, 3, self.loader.imsize, self.loader.imsize
        )
        self.x_tilde = torch.FloatTensor(
            self.loader.batchsize, 3, self.loader.imsize, self.loader.imsize
        )
        self.real_label = torch.FloatTensor(self.loader.batchsize).fill_(1)
        self.fake_label = torch.FloatTensor(self.loader.batchsize).fill_(0)

        # enable cuda
        if self.use_cuda:
            self.z = self.z.cuda()
            self.x = self.x.cuda()
            self.x_tilde = self.x.cuda()
            self.real_label = self.real_label.cuda()
            self.fake_label = self.fake_label.cuda()
            torch.cuda.manual_seed(config.random_seed)

        # wrapping autograd Variable.
        self.x = Variable(self.x)
        self.x_tilde = Variable(self.x_tilde)
        self.z = Variable(self.z)
        self.real_label = Variable(self.real_label)
        self.fake_label = Variable(self.fake_label)

        # ship new model to cuda.
        if self.use_cuda:
            self.G = self.G.cuda()
            self.D = self.D.cuda()

        # optimizer
        betas = (self.config.beta1, self.config.beta2)
        if self.optimizer == "adam":
            self.opt_g = Adam(
                filter(lambda p: p.requires_grad, self.G.parameters()),
                lr=self.lr,
                betas=betas,
                weight_decay=0.0,
            )
            self.opt_d = Adam(
                filter(lambda p: p.requires_grad, self.D.parameters()),
                lr=self.lr,
                betas=betas,
                weight_decay=0.0,
            )

    def feed_interpolated_input(self, x):
        if (
            self.phase == "gtrns"
            and floor(self.resl) > 2
            and floor(self.resl) <= self.max_resl
        ):
            alpha = self.complete["gen"] / 100.0
            transform = transforms.Compose(
                [
                    transforms.ToPILImage(),
                    transforms.Scale(
                        size=int(pow(2, floor(self.resl) - 1)), interpolation=0
                    ),  # 0: nearest
                    transforms.Scale(
                        size=int(pow(2, floor(self.resl))), interpolation=0
                    ),  # 0: nearest
                    transforms.ToTensor(),
                ]
            )
            x_low = x.clone().add(1).mul(0.5)
            for i in range(x_low.size(0)):
                x_low[i] = transform(x_low[i]).mul(2).add(-1)
            x = torch.add(x.mul(alpha), x_low.mul(1 - alpha))  # interpolated_x

        if self.use_cuda:
            return x.cuda()
        else:
            return x

    def add_noise(self, x):
        # TODO: support more method of adding noise.
        if self.flag_add_noise == False:
            return x

        if hasattr(self, "_d_"):
            self._d_ = self._d_ * 0.9 + torch.mean(self.fx_tilde).item() * 0.1
        else:
            self._d_ = 0.0
        strength = 0.2 * max(0, self._d_ - 0.5) ** 2
        z = np.random.randn(*x.size()).astype(np.float32) * strength
        z = (
            Variable(torch.from_numpy(z)).cuda()
            if self.use_cuda
            else Variable(torch.from_numpy(z))
        )
        return x + z

    def _gradient_penalty(self, gradient):
        # Gradients have shape (batch_size, num_channels, img_width, img_height),
        # so flatten to easily take norm per example in batch
        gradients = gradients.norm(2, dim=1).mean().data[0]

        # Derivatives of the gradient close to 0 can cause problems because of
        # the square root, so manually calculate norm and add epsilon
        gradients_norm = torch.sqrt(torch.sum(gradients ** 2, dim=1) + 1e-12)

        # Return gradient penalty
        return self.wgan_lambda * ((gradients_norm - 1) ** 2).mean()

    def train(self):
        # noise for test.
        self.z_test = torch.FloatTensor(self.loader.batchsize, self.nz)
        if self.use_cuda:
            self.z_test = self.z_test.cuda()

        self.z_test.data.resize_(self.loader.batchsize, self.nz).normal_(0.0, 1.0)

        for step in range(2, self.max_resl + 1 + 5):
            for iter in tqdm(
                range(
                    0,
                    (self.trns_tick * 2 + self.stab_tick * 2) * self.TICK,
                    self.loader.batchsize,
                )
            ):
                if self.just_passed:
                    continue
                self.globalIter = self.globalIter + 1
                self.stack = self.stack + self.loader.batchsize
                if self.stack > ceil(len(self.loader.dataset)):
                    self.epoch = self.epoch + 1
                    self.stack = int(self.stack % (ceil(len(self.loader.dataset))))

                # reslolution scheduler.
                self.resl_scheduler()
                if self.skip and self.previous_phase == self.phase:
                    continue
                self.skip = False
                if self.globalIter % self.accelerate != 0:
                    continue

                # zero gradients.
                self.G.zero_grad()
                self.D.zero_grad()

                # update discriminator.
                self.x.data = self.feed_interpolated_input(self.loader.get_batch())
                if self.flag_add_noise:
                    self.x = self.add_noise(self.x)
                self.z.data.resize_(self.loader.batchsize, self.nz).normal_(0.0, 1.0)
                self.x_tilde = self.G(self.z)

                self.fx = self.D(self.x)
                self.fx_tilde = self.D(self.x_tilde.detach())

                loss_d = self.mse(self.fx.squeeze(), self.real_label) + self.mse(
                    self.fx_tilde, self.fake_label
                )

                ### gradient penalty
                gradients = torch_grad(
                    outputs=self.fx,
                    inputs=self.x,
                    grad_outputs=torch.ones(self.fx.size()).cuda()
                    if self.use_cuda
                    else torch.ones(self.fx.size()),
                    create_graph=True,
                    retain_graph=True,
                )[0]
                gradient_penalty = self._gradient_penalty(gradients)
                loss_d += gradient_penalty

                ### epsilon penalty
                epsilon_penalty = torch.square(self.fx)
                loss_d += epsilon_penalty * self.wgan_epsilon
                loss_d.backward()
                self.opt_d.step()

                # update generator.
                fx_tilde = self.D(self.x_tilde)
                loss_g = self.mse(fx_tilde.squeeze(), self.real_label.detach())
                loss_g.backward()
                self.opt_g.step()

                # logging.
                if (iter - 1) % 10:
                    log_msg = " [E:{0}][T:{1}][{2:6}/{3:6}]  errD: {4:.4f} | errG: {5:.4f} | [lr:{11:.5f}][cur:{6:.3f}][resl:{7:4}][{8}][{9:.1f}%][{10:.1f}%]".format(
                        self.epoch,
                        self.globalTick,
                        self.stack,
                        len(self.loader.dataset),
                        loss_d.item(),
                        loss_g.item(),
                        self.resl,
                        int(pow(2, floor(self.resl))),
                        self.phase,
                        self.complete["gen"],
                        self.complete["dis"],
                        self.lr,
                    )
                    tqdm.write(log_msg)

                # save model.
                self.snapshot("repo/model")

                # save image grid.
                if self.globalIter % self.config.save_img_every == 0:
                    with torch.no_grad():
                        x_test = self.G(self.z_test)
                    utils.mkdir("repo/save/grid")
                    utils.mkdir("repo/save/grid_real")
                    utils.save_image_grid(
                        x_test.data,
                        "repo/save/grid/{}_{}_G{}_D{}.jpg".format(
                            int(self.globalIter / self.config.save_img_every),
                            self.phase,
                            self.complete["gen"],
                            self.complete["dis"],
                        ),
                    )
                    if self.globalIter % self.config.save_img_every * 10 == 0:
                        utils.save_image_grid(
                            self.x.data,
                            "repo/save/grid_real/{}_{}_G{}_D{}.jpg".format(
                                int(self.globalIter / self.config.save_img_every),
                                self.phase,
                                self.complete["gen"],
                                self.complete["dis"],
                            ),
                        )
                    utils.mkdir("repo/save/resl_{}".format(int(floor(self.resl))))
                    utils.mkdir("repo/save/resl_{}_real".format(int(floor(self.resl))))
                    utils.save_image_single(
                        x_test.data,
                        "repo/save/resl_{}/{}_{}_G{}_D{}.jpg".format(
                            int(floor(self.resl)),
                            int(self.globalIter / self.config.save_img_every),
                            self.phase,
                            self.complete["gen"],
                            self.complete["dis"],
                        ),
                    )
                    if self.globalIter % self.config.save_img_every * 10 == 0:
                        utils.save_image_single(
                            self.x.data,
                            "repo/save/resl_{}_real/{}_{}_G{}_D{}.jpg".format(
                                int(floor(self.resl)),
                                int(self.globalIter / self.config.save_img_every),
                                self.phase,
                                self.complete["gen"],
                                self.complete["dis"],
                            ),
                        )

                # tensorboard visualization.
                if self.use_tb:
                    with torch.no_grad():
                        x_test = self.G(self.z_test)
                    self.tb.add_scalar("data/loss_g", loss_g.item(), self.globalIter)
                    self.tb.add_scalar("data/loss_d", loss_d.item(), self.globalIter)
                    self.tb.add_scalar("tick/lr", self.lr, self.globalIter)
                    self.tb.add_scalar(
                        "tick/cur_resl", int(pow(2, floor(self.resl))), self.globalIter
                    )
                    """IMAGE GRID
                    self.tb.add_image_grid('grid/x_test', 4, utils.adjust_dyn_range(x_test.data.float(), [-1,1], [0,1]), self.globalIter)
                    self.tb.add_image_grid('grid/x_tilde', 4, utils.adjust_dyn_range(self.x_tilde.data.float(), [-1,1], [0,1]), self.globalIter)
                    self.tb.add_image_grid('grid/x_intp', 4, utils.adjust_dyn_range(self.x.data.float(), [-1,1], [0,1]), self.globalIter)
                    """
            self.just_passed = False

    def get_state(self, target):
        if target == "gen":
            state = {
                "resl": self.resl,
                "state_dict": self.G.module.state_dict(),
                "optimizer": self.opt_g.state_dict(),
            }
            return state
        elif target == "dis":
            state = {
                "resl": self.resl,
                "state_dict": self.D.module.state_dict(),
                "optimizer": self.opt_d.state_dict(),
            }
            return state

    def get_state(self, target):
        if target == "gen":
            state = {
                "resl": self.resl,
                "state_dict": self.G.module.state_dict(),
                "optimizer": self.opt_g.state_dict(),
                "globalIter": self.globalIter,
                "globalTick": self.globalTick,
            }
            return state
        elif target == "dis":
            state = {
                "resl": self.resl,
                "state_dict": self.D.module.state_dict(),
                "optimizer": self.opt_d.state_dict(),
                "globalIter": self.globalIter,
                "globalTick": self.globalTick,
            }
            return state

    def snapshot(self, path):
        if not os.path.exists(path):
            if os.name == "nt":
                os.system("mkdir {}".format(path.replace("/", "\\")))
            else:
                os.system("mkdir -p {}".format(path))
        # save every 100 tick if the network is in stab phase.
        ndis = "dis_R{}_T{}.pth.tar".format(int(floor(self.resl)), self.globalTick)
        ngen = "gen_R{}_T{}.pth.tar".format(int(floor(self.resl)), self.globalTick)
        if self.globalTick % 50 == 0:
            if self.phase == "gstab" or self.phase == "dstab" or self.phase == "final":
                save_path = os.path.join(path, ndis)
                if not os.path.exists(save_path):
                    torch.save(self.get_state("dis"), save_path)
                    save_path = os.path.join(path, ngen)
                    torch.save(self.get_state("gen"), save_path)
                    print("[snapshot] model saved @ {}".format(path))


if __name__ == "__main__":
    ## perform training.
    print("----------------- configuration -----------------")
    for k, v in vars(config).items():
        print("  {}: {}".format(k, v))
    print("-------------------------------------------------")
    torch.backends.cudnn.benchmark = True  # boost speed.
    trainer = trainer(config)
    trainer.train()
