Tutorial 2: Something a little more practical – a web page change detector
==========================================================================

.. py:currentmodule:: asphalt.core

Now that you've gone through the basics of creating an Asphalt application, it's time to
expand your horizons a little. In this tutorial you will learn to use a container
component to create a multi-component application and how to set up a configuration file
for that.

The application you will build this time will periodically load a web page and see if it
has changed since the last check. When changes are detected, it will then present the
user with the computed differences between the old and the new versions.

Setting up the project structure
--------------------------------

As in the previous tutorial, you will need a project directory and a virtual
environment. Create a directory named ``tutorial2`` and make a new virtual environment
inside it. Then activate it and use ``pip`` to install the ``asphalt-mailer`` and
``httpx`` libraries:

.. code-block:: bash

    pip install asphalt-mailer httpx

This will also pull in the core Asphalt library as a dependency.

Next, create a package directory named ``webnotifier`` and a module named ``app``
(``app.py``). The code in the following sections should be put in the ``app`` module
(unless explicitly stated otherwise).

Detecting changes in a web page
-------------------------------

The first task is to set up a loop that periodically retrieves the web page. To that
end, you need to set up an asynchronous HTTP client using the httpx_ library:

.. literalinclude:: snippets/webnotifier-app1.py
   :language: python
   :start-after: isort: off

Great, so now the code fetches the contents of ``https://imgur.com`` at 10 second
intervals. But this isn't very useful yet – you need something that compares the old and
new versions of the contents somehow. Furthermore, constantly loading the contents of a
page exerts unnecessary strain on the hosting provider. We want our application to be as
polite and efficient as reasonably possible.

To that end, you can use the ``if-modified-since`` header in the request. If the
requests after the initial one specify the last modified date value in the request
headers, the remote server will respond with a ``304 Not Modified`` if the contents have
not changed since that moment.

So, modify the code as follows:

.. literalinclude:: snippets/webnotifier-app2.py
   :language: python
   :start-after: isort: off

The code here stores the ``date`` header from the first response and uses it in the
``if-modified-since`` header of the next request. A ``200`` response indicates that the
web page has changed so the last modified date is updated and the contents are retrieved
from the response. Some logging calls were also sprinkled in the code to give you an
idea of what's happening.

.. _httpx: https://www.python-httpx.org/async/

Computing the changes between old and new versions
--------------------------------------------------

Now you have code that actually detects when the page has been modified between the
requests. But it doesn't yet show *what* in its contents has changed. The next step will
then be to use the standard library :mod:`difflib` module to calculate the difference
between the contents and send it to the logger:

.. literalinclude:: snippets/webnotifier-app3.py
   :language: python
   :start-after: isort: off

This modified code now stores the old and new contents in different variables to enable
them to be compared. The ``.split("\n")`` is needed because
:func:`~difflib.unified_diff` requires the input to be iterables of strings. Likewise,
the ``"\n".join(...)`` is necessary because the output is also an iterable of strings.

Sending changes via email
-------------------------

While an application that logs the changes on the console could be useful on its own,
it'd be much better if it actually notified the user by means of some communication
medium, wouldn't it? For this specific purpose you need the ``asphalt-mailer`` library
you installed in the beginning. The next modification will send the HTML formatted
differences to you by email.

But, you only have a single component in your app now. To use ``asphalt-mailer``, you
will need to add its component to your application somehow. This is exactly what
:meth:`Component.add_component` is for. With that, you can create a hierarchy of
components where the ``mailer`` component is a child component of your own container
component.

To use the mailer resource provided by ``asphalt-mailer``, inject it to the ``run()``
function as a resource by adding a keyword-only argument, annotated with the type of
the resource you want to inject (:class:`~asphalt.mailer.Mailer`).

And to make the the results look nicer in an email message, you can switch to using
:class:`difflib.HtmlDiff` to produce the delta output:

.. literalinclude:: snippets/webnotifier-app4.py
   :language: python
   :start-after: isort: off

You'll need to replace the ``host``, ``sender`` and ``to`` arguments for the mailer
component and possibly add the ``username`` and ``password`` arguments if your SMTP
server requires authentication.

With these changes, you'll get a new HTML formatted email each time the code detects
changes in the target web page.

Separating the change detection logic
-------------------------------------

While the application now works as intended, you're left with two small problems. First
off, the target URL and checking frequency are hard coded. That is, they can only be
changed by modifying the program code. It is not reasonable to expect non-technical
users to modify the code when they want to simply change the target website or the
frequency of checks. Second, the change detection logic is hardwired to the notification
code. A well designed application should maintain proper `separation of concerns`_. One
way to do this is to separate the change detection logic to its own class.

Create a new module named ``detector`` in the ``webnotifier`` package. Then, add the
change event class to it:

.. literalinclude:: snippets/webnotifier-detector1.py
   :language: python
   :start-after: isort: off

This class defines the type of event that the notifier will emit when the target web
page changes. The old and new content are stored in the event instance to allow the
event listener to generate the output any way it wants.

Next, add another class in the same module that will do the HTTP requests and change
detection:

.. literalinclude:: snippets/webnotifier-detector2.py
   :language: python
   :start-after: isort: off

The initializer arguments allow you to freely specify the parameters for the detection
process. The class includes a signal named ``changed`` that uses the previously created
``WebPageChangeEvent`` class. The code dispatches such an event when a change in the
target web page is detected.

Finally, add the component class which will allow you to integrate this functionality
into any Asphalt application:

.. literalinclude:: ../../examples/tutorial2/webnotifier/detector.py
   :language: python
   :start-after: isort: off

The component's ``start()`` method starts the detector's ``run()`` method as a new task,
adds the detector object as resource and installs an event listener that will shut down
the detector when the context is torn down.

Now that you've moved the change detection code to its own module,
``ApplicationComponent`` will become somewhat lighter:

.. literalinclude:: ../../examples/tutorial2/webnotifier/app.py
   :language: python
   :start-after: isort: off

The main application component will now use the detector resource added by
``ChangeDetectorComponent``. It adds one event listener which reacts to change events by
creating an HTML formatted difference and sending it to the default recipient.

Once the ``start()`` method here has run to completion, the event loop finally has a
chance to run the task created for ``Detector.run()``. This will allow the detector to
do its work and dispatch those ``changed`` events that the ``page_changed()`` listener
callback expects.

.. _separation of concerns: https://en.wikipedia.org/wiki/Separation_of_concerns

Setting up the configuration file
---------------------------------

Now that your application code is in good shape, you will need to give the user an easy
way to configure it. This is where YAML_ configuration files come in handy. They're
clearly structured and are far less intimidating to end users than program code. And you
can also have more than one of them, in case you want to run the program with a
different configuration.

In your project directory (``tutorial2``), create a file named ``config.yaml`` with the
following contents:

.. literalinclude:: ../../examples/tutorial2/config.yaml
   :language: yaml

The ``component`` section defines parameters for the root component. Aside from the
special ``type`` key which tells the runner where to find the component class, all the
keys in this section are passed to the constructor of ``ApplicationComponent`` as
keyword arguments. Keys under ``components`` will match the alias of each child
component, which is given as the first argument to :meth:`Component.add_component`. Any
component parameters given here can now be removed from the ``add_component()`` call in
``ApplicationComponent``'s code.

The logging configuration here sets up two loggers, one for ``webnotifier`` and its
descendants and another (``root``) as a catch-all for everything else. It specifies one
handler that just writes all log entries to the standard output. To learn more about
what you can do with the logging configuration, consult the
:ref:`python:logging-config-dictschema` section in the standard library documentation.

You can now run your app with the ``asphalt run`` command, provided that the project
directory is on Python's search path. When your application is `properly packaged`_ and
installed in ``site-packages``, this won't be a problem. But for the purposes of this
tutorial, you can temporarily add it to the search path by setting the ``PYTHONPATH``
environment variable:

.. code-block:: bash

    PYTHONPATH=. asphalt run config.yaml

On Windows:

.. code-block:: doscon

    set PYTHONPATH=%CD%
    asphalt run config.yaml

.. note::
    The ``if __name__ == '__main__':`` block is no longer needed since ``asphalt run``
    is now used as the entry point for the application.

.. _YAML: https://yaml.org/
.. _properly packaged: https://packaging.python.org/

Conclusion
----------

You now know how to take advantage of Asphalt's component system to add structure to
your application. You've learned how to build reusable components and how to make the
components work together through the use of resources. Last, but not least, you've
learned to set up a YAML configuration file for your application and to set up a fine
grained logging configuration in it.

You now possess enough knowledge to leverage Asphalt to create practical applications.
You are now encouraged to find out what `Asphalt component projects`_ exist to aid your
application development. Happy coding ☺

.. _Asphalt component projects: https://github.com/asphalt-framework
