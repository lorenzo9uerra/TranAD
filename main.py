import pickle
import os
import pandas as pd
from tqdm import tqdm
from src.models import *
from src.constants import *
from src.plotting import *
from src.pot import *
from src.utils import *
from src.diagnosis import *
from src.merlin import *
from torch.utils.data import Dataset, DataLoader, TensorDataset
import torch.nn as nn
from time import time
from pprint import pprint

# from beepy import beep
import wandb

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def convert_to_windows(data, model):
    windows = []
    w_size = model.n_window
    for i, g in enumerate(data):
        if i >= w_size:
            w = data[i - w_size : i]
        else:
            w = torch.cat([data[0].repeat(w_size - i, 1), data[0:i]])
        windows.append(
            w if "TranAD" in args.model or "Attention" in args.model else w.view(-1)
        )
    return torch.stack(windows)


def load_dataset(dataset):
    folder = os.path.join(output_folder, dataset)
    if not os.path.exists(folder):
        raise Exception("Processed Data not found.")
    loader = []
    for file in ["train", "test", "labels"]:
        if dataset == "SMD":
            file = "machine-1-2_" + file
        if dataset == "SMAP":
            file = "P-1_" + file
        if dataset == "MSL":
            file = "T-4_" + file
        if dataset == "UCR":
            file = "135_" + file
        if dataset == "NAB":
            file = "ec2_request_latency_system_failure_" + file
        loader.append(np.load(os.path.join(folder, f"{file}.npy")))
    # loader = [i[:, debug:debug+1] for i in loader]
    if args.less:
        loader[0] = cut_array(0.2, loader[0])
    train_loader = DataLoader(loader[0], batch_size=loader[0].shape[0])
    test_loader = DataLoader(loader[1], batch_size=loader[1].shape[0])
    labels = loader[2]
    return train_loader, test_loader, labels


def save_model(model, optimizer, scheduler, epoch, accuracy_list):
    folder = f"checkpoints/{args.model}_{args.dataset}/"
    os.makedirs(folder, exist_ok=True)
    file_path = f"{folder}/model.ckpt"
    torch.save(
        {
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
            "accuracy_list": accuracy_list,
        },
        file_path,
    )


def load_model(modelname, dims):
    import src.models

    model_class = getattr(src.models, modelname)
    model = model_class(dims).double().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=model.lr, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 5, 0.9)
    fname = f"checkpoints/{args.model}_{args.dataset}/model.ckpt"
    if os.path.exists(fname) and (not args.retrain or args.test):
        print(f"{color.GREEN}Loading pre-trained model: {model.name}{color.ENDC}")
        checkpoint = torch.load(fname)
        model.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
        epoch = checkpoint["epoch"]
        accuracy_list = checkpoint["accuracy_list"]
    else:
        print(f"{color.GREEN}Creating new model: {model.name}{color.ENDC}")
        epoch = -1
        accuracy_list = []
    return model, optimizer, scheduler, epoch, accuracy_list


def backprop(
    epoch, model, data, feats, optimizer, scheduler, device="cpu", training=True
):
    loss_fn = nn.MSELoss(reduction="mean" if training else "none")
    if "DAGMM" in model.name:
        l = nn.MSELoss(reduction="none")
        compute = ComputeLoss(model, 0.1, 0.005, "cpu", model.n_gmm)
        n = epoch + 1
        w_size = model.n_window
        l1s = []
        l2s = []
        if training:
            for d in data:
                _, x_hat, z, gamma = model(d)
                l1, l2 = l(x_hat, d), l(gamma, d)
                l1s.append(torch.mean(l1).item())
                l2s.append(torch.mean(l2).item())
                loss = torch.mean(l1) + torch.mean(l2)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            scheduler.step()
            tqdm.write(f"Epoch {epoch},\tL1 = {np.mean(l1s)},\tL2 = {np.mean(l2s)}")
            return np.mean(l1s) + np.mean(l2s), optimizer.param_groups[0]["lr"]
        else:
            ae1s = []
            for d in data:
                _, x_hat, _, _ = model(d)
                ae1s.append(x_hat)
            ae1s = torch.stack(ae1s)
            y_pred = ae1s[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            loss = l(ae1s, data)[:, data.shape[1] - feats : data.shape[1]].view(
                -1, feats
            )
            return loss.detach().numpy(), y_pred.detach().numpy()
    if "Attention" in model.name:
        l = nn.MSELoss(reduction="none")
        n = epoch + 1
        w_size = model.n_window
        l1s = []
        res = []
        if training:
            for d in data:
                ae, ats = model(d)
                # res.append(torch.mean(ats, axis=0).view(-1))
                l1 = l(ae, d)
                l1s.append(torch.mean(l1).item())
                loss = torch.mean(l1)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            # res = torch.stack(res); np.save('ascores.npy', res.detach().numpy())
            scheduler.step()
            tqdm.write(f"Epoch {epoch},\tL1 = {np.mean(l1s)}")
            return np.mean(l1s), optimizer.param_groups[0]["lr"]
        else:
            ae1s, y_pred = [], []
            for d in data:
                ae1 = model(d)
                y_pred.append(ae1[-1])
                ae1s.append(ae1)
            ae1s, y_pred = torch.stack(ae1s), torch.stack(y_pred)
            loss = torch.mean(l(ae1s, data), axis=1)
            return loss.detach().numpy(), y_pred.detach().numpy()
    elif "OmniAnomaly" in model.name:
        if training:
            mses, klds = [], []
            for i, d in enumerate(data):
                y_pred, mu, logvar, hidden = model(d, hidden if i else None)
                MSE = l(y_pred, d)
                KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=0)
                loss = MSE + model.beta * KLD
                mses.append(torch.mean(MSE).item())
                klds.append(model.beta * torch.mean(KLD).item())
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            tqdm.write(f"Epoch {epoch},\tMSE = {np.mean(mses)},\tKLD = {np.mean(klds)}")
            scheduler.step()
            return loss.item(), optimizer.param_groups[0]["lr"]
        else:
            y_preds = []
            for i, d in enumerate(data):
                y_pred, _, _, hidden = model(d, hidden if i else None)
                y_preds.append(y_pred)
            y_pred = torch.stack(y_preds)
            MSE = l(y_pred, data)
            return MSE.detach().numpy(), y_pred.detach().numpy()
    elif "USAD" in model.name:
        l = nn.MSELoss(reduction="none")
        n = epoch + 1
        w_size = model.n_window
        l1s, l2s = [], []
        if training:
            for d in data:
                ae1s, ae2s, ae2ae1s = model(d)
                l1 = (1 / n) * l(ae1s, d) + (1 - 1 / n) * l(ae2ae1s, d)
                l2 = (1 / n) * l(ae2s, d) - (1 - 1 / n) * l(ae2ae1s, d)
                l1s.append(torch.mean(l1).item())
                l2s.append(torch.mean(l2).item())
                loss = torch.mean(l1 + l2)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            scheduler.step()
            tqdm.write(f"Epoch {epoch},\tL1 = {np.mean(l1s)},\tL2 = {np.mean(l2s)}")
            return np.mean(l1s) + np.mean(l2s), optimizer.param_groups[0]["lr"]
        else:
            ae1s, ae2s, ae2ae1s = [], [], []
            for d in data:
                ae1, ae2, ae2ae1 = model(d)
                ae1s.append(ae1)
                ae2s.append(ae2)
                ae2ae1s.append(ae2ae1)
            ae1s, ae2s, ae2ae1s = (
                torch.stack(ae1s),
                torch.stack(ae2s),
                torch.stack(ae2ae1s),
            )
            y_pred = ae1s[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            loss = 0.1 * l(ae1s, data) + 0.9 * l(ae2ae1s, data)
            loss = loss[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            return loss.detach().numpy(), y_pred.detach().numpy()
    elif model.name in ["GDN", "MTAD_GAT", "MSCRED", "CAE_M"]:
        l = nn.MSELoss(reduction="none")
        n = epoch + 1
        w_size = model.n_window
        l1s = []
        if training:
            for i, d in enumerate(data):
                if "MTAD_GAT" in model.name:
                    x, h = model(d, h if i else None)
                else:
                    x = model(d)
                loss = torch.mean(l(x, d))
                l1s.append(torch.mean(loss).item())
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            tqdm.write(f"Epoch {epoch},\tMSE = {np.mean(l1s)}")
            return np.mean(l1s), optimizer.param_groups[0]["lr"]
        else:
            xs = []
            for d in data:
                if "MTAD_GAT" in model.name:
                    x, h = model(d, None)
                else:
                    x = model(d)
                xs.append(x)
            xs = torch.stack(xs)
            y_pred = xs[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            loss = l(xs, data)
            loss = loss[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            return loss.detach().numpy(), y_pred.detach().numpy()
    elif "GAN" in model.name:
        l = nn.MSELoss(reduction="none")
        bcel = nn.BCELoss(reduction="mean")
        msel = nn.MSELoss(reduction="mean")
        real_label, fake_label = torch.tensor([0.9]), torch.tensor(
            [0.1]
        )  # label smoothing
        real_label, fake_label = real_label.type(torch.DoubleTensor), fake_label.type(
            torch.DoubleTensor
        )
        n = epoch + 1
        w_size = model.n_window
        mses, gls, dls = [], [], []
        if training:
            for d in data:
                # training discriminator
                model.discriminator.zero_grad()
                _, real, fake = model(d)
                dl = bcel(real, real_label) + bcel(fake, fake_label)
                dl.backward()
                model.generator.zero_grad()
                optimizer.step()
                # training generator
                z, _, fake = model(d)
                mse = msel(z, d)
                gl = bcel(fake, real_label)
                tl = gl + mse
                tl.backward()
                model.discriminator.zero_grad()
                optimizer.step()
                mses.append(mse.item())
                gls.append(gl.item())
                dls.append(dl.item())
                # tqdm.write(f'Epoch {epoch},\tMSE = {mse},\tG = {gl},\tD = {dl}')
            tqdm.write(
                f"Epoch {epoch},\tMSE = {np.mean(mses)},\tG = {np.mean(gls)},\tD = {np.mean(dls)}"
            )
            return np.mean(gls) + np.mean(dls), optimizer.param_groups[0]["lr"]
        else:
            outputs = []
            for d in data:
                z, _, _ = model(d)
                outputs.append(z)
            outputs = torch.stack(outputs)
            y_pred = outputs[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            loss = l(outputs, data)
            loss = loss[:, data.shape[1] - feats : data.shape[1]].view(-1, feats)
            return loss.detach().numpy(), y_pred.detach().numpy()
    elif "TranAD" in model.name:
        mse_loss = nn.MSELoss(reduction="none")
        data_x = torch.DoubleTensor(data)
        dataset = TensorDataset(data_x, data_x)
        dataloader = DataLoader(dataset, batch_size=model.batch)
        epsilon = 1.25
        factor = epsilon**-epoch
        max_iters = len(dataloader)
        l1s, l2s = [], []
        if training:
            i = 1
            for d, _ in tqdm(dataloader, mininterval=2):
                window = d.permute(1, 0, 2).to(device)
                elem = (
                    window[-1, :, :].view(1, d.shape[0], feats).to(device)
                )  # [1, batch, feats]
                O1, O2, O2s = model(window, elem)
                # Loss calculated per mini-batch
                norm_01 = torch.linalg.norm(O1 - elem, ord=2, dim=2)
                norm_02 = torch.linalg.norm(O2 - elem, ord=2, dim=2)
                norm_02s = torch.linalg.norm(O2s - elem, ord=2, dim=2)
                l1 = factor * norm_01.mean() + (1 - factor) * norm_02s.mean()
                l2 = factor * norm_02.mean() - (1 - factor) * norm_02s.mean()
                optimizer.zero_grad()
                l1.backward(retain_graph=True)
                l2.backward()
                optimizer.step()
                l1s.append(l1.item())
                l2s.append(l2.item())
                if i % 100 == 0:
                    wandb.log(
                        {
                            "Mini-batch Loss 1": l1.item(),
                            "Mini-batch Loss 2": l2.item(),
                            "Iteration": (epoch * max_iters) + i,
                        }
                    )
                i += 1
            scheduler.step()
            mean_l1s = np.mean(l1s)
            mean_l2s = np.mean(l2s)
            tqdm.write(f"Epoch {epoch + 1},\tLoss 1 = {mean_l1s},\tLoss 2 = {mean_l2s}")
            return (mean_l1s, mean_l2s), optimizer.param_groups[0]["lr"]
        else:  # testing
            scores = []
            for d, _ in dataloader:
                window = d.permute(1, 0, 2).to(device)
                elem = window[-1, :, :].view(1, d.shape[0], feats).to(device)
                O1, O2, O2s = model(window, elem)
                norm_01 = torch.norm(O1 - elem, dim=2)
                norm_02s = torch.norm(O2s - elem, dim=2)
                s = (norm_01 / 2) + (norm_02s/ 2)
                scores.append(s.view(-1).cpu().detach().numpy())
            scores = np.concatenate(scores)
            return scores, None
    else:
        data = data.to(device)
        y_pred = model(data)
        loss = loss_fn(y_pred, data)
        if training:
            tqdm.write(f"Epoch {epoch},\tMSE = {loss}")
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            return loss.item(), optimizer.param_groups[0]["lr"]
        else:
            return loss.cpu().detach().numpy(), y_pred.cpu().detach().numpy()


if __name__ == "__main__":
    wandb.init(
        project="TranAD",
        config={
            "learning_rate": lr,
            "architecture": "TranAD",
            "dataset": "NF-CSE-CIC-IDS2018",
            "epochs": args.epochs,
            "encoder_layers": 2,
            "decoder_layers": 2,
            "window_size": 16,
        },
    )
    train_loader, test_loader, labels = load_dataset(args.dataset)
    if args.model in ["MERLIN"]:
        eval(f"run_{args.model.lower()}(test_loader, labels, args.dataset)")
    model, optimizer, scheduler, epoch, accuracy_list = load_model(
        args.model, labels.shape[1]
    )

    ## Prepare data
    train, test = next(iter(train_loader)), next(iter(test_loader))
    feats = train.shape[1]
    if model.name in [
        "Attention",
        "DAGMM",
        "USAD",
        "MSCRED",
        "CAE_M",
        "GDN",
        "MTAD_GAT",
        "MAD_GAN",
        "TranAD",
    ]:
        train, test = convert_to_windows(train, model), convert_to_windows(test, model)

    ### Training phase
    if not args.test:
        print(f"{color.HEADER}Training {args.model} on {args.dataset}{color.ENDC}")
        start = time()
        for e in range(epoch + 1, epoch + args.epochs + 1):
            lossT, lr = backprop(
                e, model, train, feats, optimizer, scheduler=scheduler, device=device
            )
            accuracy_list.append((lossT[0], lossT[1]))
            save_model(model, optimizer, scheduler, e, accuracy_list)
            wandb.log({"Loss 1": lossT[0], "Loss 2": lossT[1], "Epoch": e})
        print(
            color.BOLD
            + "Training time: "
            + "{:10.4f}".format(time() - start)
            + " s"
            + color.ENDC
        )
        plot_accuracies(accuracy_list, f"{args.model}_{args.dataset}")

    ### Testing phase
    torch.zero_grad = True
    model.eval()
    print(f"{color.HEADER}Testing {args.model} on {args.dataset}{color.ENDC}")
    scores, _ = backprop(
        0,
        model,
        test,
        feats,
        optimizer,
        scheduler=scheduler,
        device=device,
        training=False,
    )

    # ## Plot curves
    # if not args.test:
    # 	if 'TranAD' in model.name: testO = torch.roll(test, 1, 0)
    # 	plotter(f'{args.model}_{args.dataset}', testO, y_pred, loss, labels)

    ### Scores
    scoresT, _ = backprop(
        0, model, train, feats, optimizer, scheduler, device, training=False
    )
    results = []
    scores, scoresT = scores.reshape(-1, 1), scoresT.reshape(-1, 1)
    for i in range(scores.shape[1]):
        lt, l, ls = scoresT[:, i], scores[:, i], labels[:, i]
        result, pred = pot_eval(lt, l, ls)
        preds.append(pred)
        results.append(result)
    df = pd.DataFrame(results)
    scoresTfinal, scoresFinal = np.mean(scoresT, axis=1), np.mean(scores, axis=1)
    labelsFinal = (np.sum(labels, axis=1) >= 1) + 0
    result, _ = pot_eval(scoresTfinal, scoresFinal, labelsFinal)
    result.update(hit_att(scores, labels))
    result.update(ndcg(scores, labels))
    print(df)
    pprint(result)
    wandb.log(result)
    wandb.finish()
