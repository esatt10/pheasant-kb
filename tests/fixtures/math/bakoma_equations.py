"""Regenerates bakoma_equations.pdf: ``python bakoma_equations.py OUT.pdf 3``."""

import sys

import matplotlib

matplotlib.use("pdf")
import matplotlib.pyplot as plt  # noqa: E402

matplotlib.rcParams["mathtext.fontset"] = "cm"
matplotlib.rcParams["font.family"] = "serif"
matplotlib.rcParams["font.serif"] = ["cmr10"]
matplotlib.rcParams["axes.formatter.use_mathtext"] = True
matplotlib.rcParams["pdf.fonttype"] = int(sys.argv[2])
lines = [
    (r"Energy is $E = mc^2$ and the ratio is $\varphi = \frac{1+\sqrt{5}}{2}$.", 14),
    (
        r"The sample mean is $\bar{x} = \frac{1}{n}\sum_{i=1}^{n} x_i$"
        r" for data $x_1, \ldots, x_n$.",
        14,
    ),
    (r"$\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}$", 18),
    (r"Bayes: $p(\theta \mid x) = \frac{p(x \mid \theta)\,p(\theta)}{p(x)}$.", 14),
    (r"$\nabla \cdot \mathbf{E} = \frac{\rho}{\varepsilon_0}$", 18),
    (r"$\hat{\beta} = (X^T X)^{-1} X^T y$ and $\alpha \leq \beta \neq \gamma$", 14),
    (r"$\sum_{k=0}^{n} a_k = 2^n \qquad \prod_{j=1}^{m} a_j \leq \infty$", 18),
]
fig = plt.figure(figsize=(8.5, 11))
y = 0.95
for text, size in lines:
    fig.text(0.08, y, text, fontsize=size)
    y -= 0.08
fig.savefig(sys.argv[1])
