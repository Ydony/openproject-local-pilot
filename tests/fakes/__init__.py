"""Test fakes: scripted stand-ins, never touching Docker, network or real APIs."""

from tests.fakes.http_fake import FakeServer

__all__ = ["FakeServer"]
