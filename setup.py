# -*- coding: utf-8 -*-

# DO NOT EDIT THIS FILE!
# This file has been autogenerated by dephell <3
# https://github.com/dephell/dephell

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup


import os.path

readme = ""
here = os.path.abspath(os.path.dirname(__file__))
readme_path = os.path.join(here, "README.rst")
if os.path.exists(readme_path):
    with open(readme_path, "rb") as stream:
        readme = stream.read().decode("utf8")


setup(
    long_description=readme,
    name="lightbus",
    version="1.0.1",
    description="RPC & event framework for Python 3",
    python_requires=">=3.7",
    project_urls={
        "documentation": "https://lightbus.org",
        "homepage": "https://lightbus.org",
        "repository": "https://github.com/adamcharnock/lightbus/",
    },
    author="Adam Charnock",
    author_email="adam@adamcharnock.com",
    keywords="python messaging redis bus queue",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Framework :: AsyncIO",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: POSIX",
        "Programming Language :: Python :: 3",
        "Topic :: System :: Networking",
        "Topic :: Communications",
    ],
    entry_points={
        "console_scripts": ["lightbus = lightbus.commands:lightbus_entry_point"],
        "lightbus_event_transports": [
            "debug = lightbus:DebugEventTransport",
            "redis = lightbus:RedisEventTransport",
        ],
        "lightbus_plugins": [
            "internal_metrics = lightbus.plugins.metrics:MetricsPlugin",
            "internal_state = lightbus.plugins.state:StatePlugin",
        ],
        "lightbus_result_transports": [
            "debug = lightbus:DebugResultTransport",
            "redis = lightbus:RedisResultTransport",
        ],
        "lightbus_rpc_transports": [
            "debug = lightbus:DebugRpcTransport",
            "redis = lightbus:RedisRpcTransport",
        ],
        "lightbus_schema_transports": [
            "debug = lightbus:DebugSchemaTransport",
            "redis = lightbus:RedisSchemaTransport",
        ],
    },
    packages=[
        "lightbus",
        "lightbus.client",
        "lightbus.client.docks",
        "lightbus.client.internal_messaging",
        "lightbus.client.subclients",
        "lightbus.commands",
        "lightbus.config",
        "lightbus.plugins",
        "lightbus.schema",
        "lightbus.serializers",
        "lightbus.transports",
        "lightbus.transports.redis",
        "lightbus.utilities",
    ],
    package_dir={"": "."},
    package_data={},
    install_requires=["aioredis>=1.2.0", "jsonschema>=3.2", "pyyaml>=3.12"],
)
