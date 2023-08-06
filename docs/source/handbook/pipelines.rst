Pipelines
---------

Pipelines expose an API "similar" to :class:`~coredis.Redis` with the exception
that calling any redis command returns the pipeline instance itself.

To retrieve the actual results of each command queued in the pipeline you must call
:meth:`~coredis.pipeline.Pipeline.execute`

For example:


.. code-block:: python

    async def example(client):
        async with await client.pipeline() as pipe:
            await pipe.delete(['bar'])
            await pipe.set('bar', 'foo')
            await pipe.execute()  # needs to be called explicitly


Here are more examples:


.. code-block:: python

    async def example(client):
        async with await client.pipeline(transaction=True) as pipe:
            # will return self to send another command
            pipe = await (await pipe.flushdb()).set('foo', 'bar')
            # can also directly send command
            await pipe.set('bar', 'foo')
            # commands will be buffered
            await pipe.keys('*')
            res = await pipe.execute()
            # results should be in order corresponding to your command
            assert res == (True, True, True, set([b'bar', b'foo']))

For ease of use, all commands being buffered into the pipeline return the
pipeline object itself. Which enable you to use it like the example provided.

Atomicity & Transactions
^^^^^^^^^^^^^^^^^^^^^^^^
In addition, pipelines can also ensure the buffered commands are executed
atomically as a group by using the :paramref:`~coredis.Redis.pipeline.transaction` argument.

.. code-block:: python

    pipe = r.pipeline(transaction=True)

A common issue occurs when requiring atomic transactions but needing to
retrieve values in Redis prior for use within the transaction. For instance,
let's assume that the :rediscommand:`INCR` command didn't exist and we need to build an atomic
version of :rediscommand:`INCR` in Python.

The completely naive implementation could :rediscommand:`GET` the value, increment it in
Python, and :rediscommand:`SET` the new value back. However, this is not atomic because
multiple clients could be doing this at the same time, each getting the same
value from :rediscommand:`GET`.

Enter the :rediscommand:`WATCH` command. :rediscommand:`WATCH` provides the ability to monitor one or more keys
prior to starting a transaction. If any of those keys change prior the
execution of that transaction, the entire transaction will be canceled and a
:exc:`~coredis.exceptions.WatchError` will be raised. To implement our own client-side :rediscommand:`INCR` command, we
could do something like this:

.. code-block:: python

    async def example():
        async with await r.pipeline() as pipe:
            while True:
                try:
                    # put a WATCH on the key that holds our sequence value
                    await pipe.watch('OUR-SEQUENCE-KEY')
                    # after WATCHing, the pipeline is put into immediate execution
                    # mode until we tell it to start buffering commands again.
                    # this allows us to get the current value of our sequence
                    current_value = await pipe.get('OUR-SEQUENCE-KEY')
                    next_value = int(current_value) + 1
                    # now we can put the pipeline back into buffered mode with MULTI
                    pipe.multi()
                    await pipe.set('OUR-SEQUENCE-KEY', next_value)
                    # and finally, execute the pipeline (the set command)
                    await pipe.execute()
                    # if a WatchError wasn't raised during execution, everything
                    # we just did happened atomically.
                    break
                except WatchError:
                    # another client must have changed 'OUR-SEQUENCE-KEY' between
                    # the time we started WATCHing it and the pipeline's execution.
                    # our best bet is to just retry.
                    continue

Note that, because the Pipeline must bind to a single connection for the
duration of a :rediscommand:`WATCH`, care must be taken to ensure that the connection is
returned to the connection pool by calling the :meth:`~coredis.pipeline.Pipeline.reset` method. If the
:class:`~coredis.pipeline.Pipeline` is used as a context manager (as in the example above) :meth:`~coredis.pipeline.Pipeline.reset`
will be called automatically. Of course you can do this the manual way by
explicitly calling :meth:`~coredis.pipeline.Pipeline.reset`:

.. code-block:: python

    async def example():
        async with await r.pipeline() as pipe:
            while 1:
                try:
                    await pipe.watch('OUR-SEQUENCE-KEY')
                    ...
                    await pipe.execute()
                    break
                except WatchError:
                    continue
                finally:
                    await pipe.reset()

A convenience method :meth:`~coredis.Redis.transaction` exists for handling all the
boilerplate of handling and retrying watch errors. It takes a callable that
should expect a single parameter, a pipeline object, and any number of keys to
be watched. Our client-side :rediscommand:`INCR` command above can be written like this,
which is much easier to read:

.. code-block:: python

    async def client_side_incr(pipe) -> int:
        current_value = await pipe.get('OUR-SEQUENCE-KEY') or 0
        next_value = int(current_value) + 1
        pipe.multi()
        await pipe.set('OUR-SEQUENCE-KEY', next_value)
        return next_value

    await r.transaction(client_side_incr, 'OUR-SEQUENCE-KEY')
    # (True,)
    await r.transaction(client_side_incr, 'OUR-SEQUENCE-KEY', value_from_callable=True)
    # 2


