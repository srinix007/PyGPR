import torch as tc
import numpy as np
import scipy.optimize as opt
import matplotlib.pyplot as plt
import opt_einsum as oes

tc.set_default_tensor_type(tc.DoubleTensor)


class GPR(object):
    def __init__(self, x, y, cov, hp=None, **kargs):

        self.x = tc.clone(x)
        self.y = tc.clone(y)
        self.cov = cov
        if hp is None:
            self.hp = self.cov(x)
        else:
            self.hp = hp
        self.args = kargs

        self.krn = NotImplemented
        self.wt = NotImplemented
        self.krnchd = NotImplemented

        self.need_upd = True

        self.dgn = {}

    def cost_fun(self, hp):
        f = log_likelihood(self.x, self.y, hp, self.cov, **self.args)
        return f

    def jac_cost_fun(self, hp):
        jac_f = jac_log_likelihood(self.x, self.y, hp, self.cov, **self.args)
        return jac_f

    def train(self, method='CG', jac=True):

        if jac:
            res = opt.minimize(self.cost_fun,
                               self.hp,
                               jac=self.jac_cost_fun,
                               method=method)
        else:
            res = opt.minimize(self.cost_fun,
                               self.hp,
                               jac=False,
                               method=method)
        self.hp = res.x
        self.need_upd = True

        self.llhd = res.fun
        self.jac_llhd = res.jac

        return res

    def get_pred_covar(self, xs, krns, krnchd=None, hp=None):
        if hp is None:
            hp = self.hp

        if krnchd is None:
            krnchd = self.krnchd

        krnss = self.cov(xs, hp=hp, **self.args)
        lks = tc.cholesky_solve(krns.transpose(0, 1), krnchd)
        covars = krnss.sub_(
            tc.bmm(krns[None, :, :], lks[None, :, :]).squeeze())

        return covars

    def interpolate(self, xs, skip_var=False):

        if self.need_upd:
            self.krn = self.cov(self.x, hp=self.hp, **self.args)
            self.krnchd = tc.cholesky(self.krn)
            self.wt = tc.squeeze(
                tc.cholesky_solve(self.y.reshape(-1, 1), self.krnchd))
            self.need_upd = False

        krns = self.cov(self.x, xs=xs, hp=self.hp, **self.args)
        ys = tc.mv(krns, self.wt)

        if not skip_var:
            covars = self.get_pred_covar(xs, krns)
            return ys, covars
        else:
            return ys

    def diagnostics(self, xs, ys, covar, ya, diag=False):
        var = tc.diag(covar)
        n = ys.shape[0]
        err = ys - ya

        self.dgn['RMSE'] = tc.sqrt(tc.mean(tc.sum(err**2)))
        self.dgn['SDSUM'] = tc.sqrt(tc.mean(tc.sum(var)))
        self.dgn['RCHI-SQ'] = (1.0 / n) * tc.sum((err**2) / var)

        if diag == True:
            self.dgn['LLHD'] = -0.5 * tc.sum(np.log(var)) \
                - 0.5 * tc.log(2 * np.pi) - n * self.dgn['RCHI-SQ']
        else:
            eig, evec = tc.symeig(covar)
            sol, lu = tc.solve(err[:, np.newaxis], covar)
            md = tc.dot(err, sol.squeeze_())
            self.dgn['LLHD'] = -0.5 * tc.sum(tc.log(eig)) \
                - 0.5 * tc.log(tc.tensor(2 * np.pi)) - md
            self.dgn['MD'] = (1.0 / n) * md

    def plot_ci(self, ys, ya, ax=None):
        min_ys = tc.min(ys)
        max_ys = tc.max(ys)
        ax.scatter(ys, ya, color='red', label='predicted Vs exact')
        ax.plot([min_ys, max_ys], [min_ys, max_ys])
        ax.axis('equal')
        ax.legend()

    def plot_hist_sig(self, ys, covar, diag=False, ax=None):
        if diag:
            sig = tc.sqrt(covar).div_(ys)
        else:
            sig = tc.sqrt(tc.diag(covar)).div_(ys)

        ax.hist(tc.log(sig), label='log($\\frac{\sigma}{\mu}$)')
        ax.legend()

    def plot_hist_err(self, ys, ya, ax=None):
        ax.hist(tc.log(ys - ya) - tc.log(ya), label='log(rel error)')
        ax.legend()

    def plot_hparam(self, ax=None):
        ax.scatter(range(0, len(self.hp)), self.hp, label='$\\theta$')
        ax.set(xlabel='S.No')
        ax.legend()

    def plot_jac(self, ax=None):
        ax.scatter(range(0, len(self.hp)),
                   np.log(np.abs(self.jac_llhd)),
                   label='-log($dL/d\\theta$)')
        ax.set(xlabel='S.No')
        ax.legend()

    def plot(self, xs, ys, covars, ya, diag=False):

        fig = plt.figure(constrained_layout=True)
        gs = fig.add_gridspec(2, 2, wspace=0.2, hspace=0.2)

        pred = fig.add_subplot(gs[0, 0])
        sigma = fig.add_subplot(gs[0, 1])
        hpar = fig.add_subplot(gs[1, 0].subgridspec(2, 1)[0])
        jac = fig.add_subplot(gs[1, 0].subgridspec(2, 1)[1])
        mse = fig.add_subplot(gs[1, 1])

        self.plot_ci(ys, ya, ax=pred)
        self.plot_hist_sig(ys, covars, ax=sigma)
        self.plot_hparam(ax=hpar)
        self.plot_jac(ax=jac)
        self.plot_hist_err(ys, ya, ax=mse)


def log_likelihood(x, y, hp, cov, **kwargs):

    krn = cov(x, hp=hp, **kwargs)
    krnchd = tc.cholesky(krn)

    wt = tc.squeeze(tc.cholesky_solve(y.reshape(-1, 1), krnchd))

    llhd = 0.5 * tc.dot(wt, y) \
        + tc.sum(tc.log(tc.diag(krnchd))) \
        + 0.5 * len(y) * tc.log(tc.tensor(2 * np.pi))

    return llhd.numpy()


def jac_log_likelihood(x, y, hp, cov, **kwargs):

    krn, dkrn = cov(x, hp=hp, deriv=True, **kwargs)
    krnchd = tc.cholesky(krn)

    wt = tc.cholesky_solve(y.reshape(-1, 1), krnchd).squeeze_()
    kk = tc.cholesky_solve(dkrn, krnchd)

    tr1 = oes.contract('i,kij,j->k', wt, dkrn, wt, backend='torch')
    tr2 = tc.diagonal(kk, dim1=-1, dim2=-2).sum(-1)

    jac_llhd = tr1.sub_(tr2).mul_(-0.5)

    return jac_llhd.numpy()
