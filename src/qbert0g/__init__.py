"""Qbert0G — gRPC service streaming freshly measured quantum noise.

Serves two gRPC services from one process:

- ``qrng.QuantumRNG`` — the public, general-purpose service
  (``GetRandomBytes``).
- ``qr_entropy.EntropyService`` — the qr-sampler seam
  (``GetEntropy`` unary + ``StreamEntropy`` bidi with ``sequence_id``
  echo and nanosecond generation timestamps).

Importing this package has no side effects: no config is read, no
database or device is opened. Everything is wired explicitly in
:mod:`qbert0g.server` from a :class:`qbert0g.config.Config`.
"""

__version__ = "1.0.0"
