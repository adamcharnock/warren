# Anatomy lesson

Lightbus provides you with two tools:

* A **client** with which to fire [events], listen for events
  and make [remote procedure calls] (RPCs).
* A **stand-alone Lightbus worker process** in which you can setup
  event listeners. This process will also respond to RPCs calls.

![A simple Lightbus deployment][simple-processes]

[simple-processes]: /static/images/simple-processes.png

## The client

The client allows you to interact with the bus from within your Python
codebase. For example:

```python3
# The bus is created in your bus.py file
import lightbus

bus = lightbus.create()
```

```python3
# Elsewhere in codebase
from my_project.bus import bus

# Perform a remote procedure call
is_valid = bus.auth.check_password(
    user="admin",
    password="secret"
)

# Fire an event
bus.auth.user_registered(
    user="sally",
    email="sally@example.com"
)
```

You can use this client anywhere you need to, such as:

* Within your Django/Flask views
* Within scheduled jobs
* Within Lightbus event & RPC handlers (see below)

!!! important

    Each service should create its bus client within the service's `bus.py` file (also known as the servies *bus module*). 
    Other code within the service should import the bus client from the bus module as needed.
    See [how to access your bus client](../howto/access-your-bus-client.md).


## The Lightbus worker process (`lightbus run`)

The Lightbus worker is a long running process started using the `lightbus run` console command. 
This process serves two purposes:

* Listens for events and executes any listeners you have created.
* Respond to incoming remote procedure calls for the service's registered APIs.

This process imports your bus module (see the [module loading configuration] reference) 
in order to bootstrap itself. Your bus module should therefore:

1. Instantiate the `bus` client in a module variable named `bus`
1. Register any API definitions for your service
1. Setup listeners for any events you wish to listen for

For example, let's use the `auth.create_user()` remote procedure call
to create a new user every time a `customers.new_customer` event appears on the
bus:

```python3
# bus.py
import lightbus

bus = lightbus.create()

def create_user_for_customer(event_message,
                             customer_name, email):
    # We can do something locally, or call an
    # RPC, or both. Here we call an RPC.
    bus.auth.create_user(
        name=customer_name,
        email=email
    )

# Setup our listeners on startup
@bus.client.on_start()
def on_start():
    # Create a new user for each new customer
    bus.customers.new_customer.listen(
        create_user_for_customer
    )
```

You start this process using the command:

    lightbus run


Lightbus will import the bus module (your `bus.py` file) and wait
for incoming events and remote procedure calls.

**A service
will only need a Lightbus process if it wishes to listen
for [events] or provide any [RPCs] which can be called.**

[service]: concepts.md#service
[events]: events.md
[rpcs]: rpcs.md
[remote procedure calls]: rpcs.md
[module loading configuration]: ../reference/configuration.md#1-module-loading
