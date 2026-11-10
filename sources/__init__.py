"""Provider audio di GrabMedia.

Ogni modulo in questa cartella (es. ``sources/<nome>.py``) definisce una o più
sottoclassi di :class:`sources.base.AudioSource`. Il core li carica in modo lazy
tramite :mod:`core.registry`; un modulo che non si importa non blocca gli altri.
"""
