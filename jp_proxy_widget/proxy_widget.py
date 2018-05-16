
"""
This is an implementation of a generic "javascript proxy" Jupyter notebook widget.
The idea is that for many purposes this widget will make it easy to use javascript
components without having to implement the "javascript view" side of the widget.

For example to create a jqueryui dialog we don't need any javascript support
because jqueryui is already loaded as part of Jupyter and the proxy widget
supplies access to the needed methods from Python:

     from jp_gene_viz import js_proxy
     from IPython.display import display
     js_proxy.load_javascript_support()
     dialog = js_proxy.ProxyWidget()
     command = dialog.element().html("Hello from jqueryui").dialog()
     display(dialog)
     dialog.send_command(command)

The strategy is to pass a sequence of encoded javascript "commands" as a JSON
object to the generic widget proxy and have the proxy execute javascript actions
in response to the commands.  Commands can be chained using "chaining".

(object.method1(...).
    method2(...).
    method3(...)
    )

Results for the last command of the chain are passed back to the
Python widget controller object (to a restricted recursive depth)
except that non-JSON permitted values are mapped to None.

Here are notes on the encoding function E for the JSON commands and their interpretation
as javascript actions:

WIDGET INTERFACE: widget.element()
JSON ENCODING: ["element"]
JAVASCRIPT ACTION: get the this.$el element for the widget.
JAVASCRIPT RESULT: this.$el
PASSED TO PYTHON: This should never be the end of the chain!

WIDGET INTERFACE: widget.window()
JSON ENCODING: ["window"]
JAVASCRIPT ACTION: get the global namespace (window object)
JAVASCRIPT RESULT: window object
PASSED TO PYTHON: This should never be the end of the chain!

WIDGET INTERFACE: <target>.method(<arg0>, <arg1>, ..., <argn>)
  or for non-python names <target>.__getattr___("$method")(<arg0>...)
JSON ENCODING: ["method", target, method_name, arg0, ..., argn]
JAVASCRIPT ACTION: E(target).method_name(E(arg0), E(arg1), ..., E(argn))
JAVASCRIPT RESULT: Result of method call.
PASSED TO PYTHON: Result of method call in JSON translation.

WIDGET INTERFACE: (this is not exposed to the widget directly)
JSON ENCODING: ["id", X]
JAVASCRIPT ACTION/RESULT: X -- untranslated JSON object.
PASSED TO PYTHON: X (but it should never be the last in the chain)

WIDGET INTERFACE: (not exposed)
JSON ENCODING: ["list", x0, x1, ..., xn]
JAVASCRIPT ACTION/RESULT: [E[x0], E[x1], ..., E[xn]]  -- recursively translated list.
PASSED TO PYTHON: should never be returned.

WIDGET INTERFACE: (not exposed)
JSON ENCODING: ["dict", {k0: v0, ..., kn: vn}]
JAVASCRIPT ACTION/RESULT: {k0: E(v0), ..., kn: E(vn)} -- recursively translated mapping.
PASSED TO PYTHON: should never be returned.

WIDGET INTERFACE: widget.callback(function, untranslated_data, depth=1)
JSON ENCODING: ["callback", numerical_identifier, untranslated_data]
JAVASCRIPT ACTION: create a javascript callback function which triggers 
   a python call to function(js_parameters, untranslated_data).
   The depth parameter controls the recursion level for translating the
   callback parameters to JSON when they are passed back to Python.
   The callback function should have the signature
       callback(untranslated_data, callback_arguments_json)
PASSED TO PYTHON: should never be returned.

WIDGET INTERFACE: target.attribute_name
   or for non-python names <target>.__getattr___("$attr")
JSON ENCODING: ["get", target, attribute_name]
JAVASCRIPT ACTION/RESULT: E(target).attribute_name
PASSED TO PYTHON: The value of the javascript property

WIDGET INTERFACE: <target>._set(attribute_name, <value>)
JSON ENCODING: ["set", target, attribute_name, value]
JAVASCRIPT ACTION: E(target).attribute_name = E(value)
JAVASCRIPT RESULT: E(target) for chaining.
PASSED TO PYTHON: E(target) translated to JSON (probably should never be last in chain)

WIDGET INTERFACE: not directly exposed.
JSON ENCODING: not_a_list
JAVASCRIPT ACTION: not_a_list -- other values are not translated
PASSED TO PYTHON: This should not be the end of the chain.

WIDGET INTERFACE: widget.new(<target>. <arg0>, ..., <argn>)
JSON ENCODING: ["new", target, arg0, ... argn]
JAVASCRIPT ACTION: thing = E(target); result = new E(arg0, ... argn)
PASSED TO PYTHON: This should not be the end of the chain.

WIDGET INTERFACE: <target>._null.
JSON ENCODING: ["null", target]
JAVASCRIPT ACTION: execute E(target) and discard the final value to prevent 
   large structures from propagating when not needed.  This is an 
   optimization to prevent unneeded communication.
PASSED TO PYTHON: None

"""

#print "this should be a syntax error in py3"

import ipywidgets as widgets
from traitlets import Unicode
import time
import IPython
from IPython.display import display, HTML
import traitlets
import json
#import threading
import types
import traceback
from . import js_context
from .hex_codec import hex_to_bytearray, bytearray_to_hex


# xxxx remove this?
def load_components(verbose=False):
    # shortcut will not work correctly if window has been reloaded.
    #if JSProxyWidget._jqueryUI_checked and JSProxyWidget._require_checked:
    #    if verbose:
    #        print("Components loaded previously.")
    #    return
    w = JSProxyWidget.shared_loader_widget(verbose)
    # add require after adding jquery
    #   xxxx really should add registration for jquery and jqueryui with requirejs if it is available...
    def jquery_loaded():
        if verbose:
            print ("jQuery and jQueryUI loaded")
    w.check_jquery(onsuccess=jquery_loaded, verbose=verbose)
    def requirejs_loaded():
        if verbose:
            print ("requirejs loaded")
    w._check_require_is_loaded(onsuccess=requirejs_loaded)


# In the IPython context get_ipython is a builtin.
# get a reference to the IPython notebook object.
ip = IPython.get_ipython()

JAVASCRIPT_EMBEDDING_TEMPLATE = u"""
(function () {{
    {debugger_string}
    var do_actions = function () {{
        var element = $("#{div_id}");
        // define special functions...
        element.New = function(klass, args) {{
            var obj = Object.create(klass.prototype);
            return klass.apply(obj, args) || obj;
        }};
        element.Fix = function () {{
            // do nothing (not implemented.)
        }}
        var f;  // named function variable for debugging.
        {actions};
    }};
    var wait_for_libraries = function () {{
        var names = {names};
        for (var i=0; i<names.length; i++) {{
            var library = undefined;
            try {{
                library = eval(names[i]);
            }} catch (e) {{
                // do nothing
            }}
            if ((typeof library) == "undefined") {{
                return window.setTimeout(wait_for_libraries, 500);
            }}
        }}
        return do_actions();
    }};
    wait_for_libraries();
}})();
"""

HTML_EMBEDDING_TEMPLATE = (u"""
<div id="{div_id}"></div>
<script>""" +
JAVASCRIPT_EMBEDDING_TEMPLATE + """
</script>
""")

# For creating unique DOM identities for embedded objects
IDENTITY_COUNTER = [int(time.time()) % 10000000]

# String constants for messaging
INDICATOR = "indicator"
PAYLOAD = "payload"
RESULTS = "results"
CALLBACK_RESULTS = "callback_results"
JSON_CB_FRAGMENT = "jcb_results"
JSON_CB_FINAL = "jcb_final"
COMMANDS = "commands"
COMMANDS_FRAGMENT = "cm_fragment"
COMMANDS_FINAL = "cm_final"

# message egmentation size default
BIG_SEGMENT = 1000000

@widgets.register
class JSProxyWidget(widgets.DOMWidget):
    """Introspective javascript proxy widget."""
    _view_name = Unicode('JSProxyView').tag(sync=True)
    _model_name = Unicode('JSProxyModel').tag(sync=True)
    _view_module = Unicode('jp_proxy_widget').tag(sync=True)
    _model_module = Unicode('jp_proxy_widget').tag(sync=True)
    _view_module_version = Unicode('^0.1.0').tag(sync=True)
    _model_module_version = Unicode('^0.1.0').tag(sync=True)

    # traitlet port to use for sending commends to javascript
    #commands = traitlets.List([], sync=True)

    # Rendered flag sent by JS view after render is complete.
    rendered = traitlets.Bool(False, sync=True)

    status = traitlets.Unicode("Not initialized")

    error_msg = traitlets.Unicode("No error", sync=True)

    # traitlet port to receive results of commands from javascript
    #results = traitlets.List([], sync=True)

    # traitlet port to receive results of callbacks from javascript
    #callback_results = traitlets.List([], sync=True)

    verbose = False

    # set to automatically flush messages to javascript side without buffering
    auto_flush = True

    def __init__(self, *pargs, **kwargs):
        super(JSProxyWidget, self).__init__(*pargs, **kwargs)
        self.counter = 0
        self.count_to_results_callback = {}
        self.default_event_callback = None
        self.identifier_to_callback = {}
        #self.callback_to_identifier = {}
        #self.on_trait_change(self.handle_callback_results, "callback_results")
        #self.on_trait_change(self.handle_results, "results")
        self.on_trait_change(self.handle_rendered, "rendered")
        ##pr "registered on_msg(handle_custom_message)"
        self.on_msg(self.handle_custom_message_wrapper)
        self.buffered_commands = []
        self.commands_awaiting_render = []
        self.last_commands_sent = []
        self.last_callback_results = None
        self.results = []
        self.status = "Not yet rendered"

    # widget used to load javascript components (shared)
    _shared_loader_widget = None

    @classmethod
    def shared_loader_widget(cls, verbose=False):
        # xxxxx remove this?
        w = cls._shared_loader_widget
        if not w:
            if verbose:
                print("initializing loader widget") 
            w = cls()
            cls._shared_loader_widget = w
            w.visible = False
            show = w
            w.js_init("element.html('&nbsp;')")  # "no" visible content for widget
            if verbose:
                show = w.debugging_display(tagline="Checking/loading javascript helpers for proxy widgets:")
            JSProxyWidget._shared_loader_widget = w
            display(show)
        elif verbose:
            print("using existing shared loading widget.")
        return w

    def js_init(self, js_function_body, callable_level=3, **other_arguments):
        """
        Run special purpose javascript initialization code.
        The function body is provided with the element as a free variable.
        """
        #pr ("js_init")
        #pr(js_function_body)
        other_argument_names = list(other_arguments.keys())
        def map_value(v):
            if callable(v):
                return self.callable(v, level=callable_level)
            return v
        other_argument_values = [map_value(other_arguments[name]) for name in other_argument_names]
        argument_names = list(["element"] + other_argument_names)
        argument_values = list([self.element()] + other_argument_values)
        function = self.function(argument_names, js_function_body)
        function_call = function(*argument_values)
        # execute the function call on the javascript side.
        self(function_call)
        #self.flush()

    _load_components_when_rendered = False

    def load_components(self, verbose=False, delay=15):
        if not self.rendered:
            # Loading components won't work until the element has been rendered.
            self._load_components_when_rendered = True
        else:
            self.status = "loading components"
            self.check_jquery(verbose=verbose)
            self._check_require_is_loaded(verbose=verbose)
            self.status = ("components loaded delaying " +
                repr(len(self.commands_awaiting_render)))
            time.sleep(delay)
            self.status = "done loading components"

    def handle_rendered(self, att_name, old, new):
        "when rendered send any commands awaiting the render event."
        # load components before sending commands
        if self._load_components_when_rendered:
            self.load_components()
        if self.commands_awaiting_render:
            self.status = "now sending commands"
            self.send_commands([])
        self.status= "Rendered."

    def send_custom_message(self, indicator, payload):
        package = { 
            INDICATOR: indicator,
            PAYLOAD: payload,
        }
        self.send(package)

    # slot for last message data debugging
    _last_message_data = None
    _json_accumulator = []
    _last_custom_message_error = None
    _last_accumulated_json = None
    
    # Output context for message handling -- will print exception traces, for example, if set
    output = None 

    def handle_custom_message_wrapper(self, widget, data, *etcetera):
        "wrapper to enable output redirects for custom messages."
        output = self.output
        if output is not None:
            with output:
                self.handle_custom_message(widget, data, *etcetera)
        else:
            self.handle_custom_message(widget, data, *etcetera)

    def debugging_display(self, tagline="debug message area for widget:", border='1px solid black'):
        if border:
            out = widgets.Output(layout={'border': border})
        else:
            out = widgets.Output()
        if tagline:
            with out:
                print (tagline)
        self.output = out
        status_text = widgets.Text(description="status:", value="")
        traitlets.directional_link((self, "status"), (status_text, "value"))
        error_text = widgets.Text(description="error", value="")
        traitlets.directional_link((self, "error_msg"), (error_text, "value"))
        assembly = widgets.VBox(children=[self, status_text, error_text, out])
        return assembly

    def handle_custom_message(self, widget, data, *etcetera):
        try:
            self._last_message_data = data
            indicator = data[INDICATOR];
            payload = data[PAYLOAD]
            if indicator == RESULTS:
                self.results = payload
                self.status = "Got results."
                self.handle_results(payload)
            elif indicator == CALLBACK_RESULTS:
                self.status = "got callback results"
                self.last_callback_results = payload
                self.handle_callback_results(payload)
            elif indicator == JSON_CB_FRAGMENT:
                self.status = "got callback fragment"
                self._json_accumulator.append(payload)
            elif indicator == JSON_CB_FINAL:
                self.status = "got callback final"
                acc = self._json_accumulator
                self._json_accumulator = []
                acc.append(payload)
                self._last_accumulated_json = acc
                accumulated_json_str = u"".join(acc)
                accumulated_json_ob = json.loads(accumulated_json_str)
                self.handle_callback_results(accumulated_json_ob)
            else:
                self.status = "Unknown indicator from custom message " + repr(indicator)
        except Exception as e:
            # for debugging assistance
            #pr ("custom message error " + repr(e))
            self._last_custom_message_error = e
            self.error_msg = repr(e)
            raise

    def unique_id(self, prefix="jupyter_proxy_widget_id_"):
        IDENTITY_COUNTER[0] += 1
        return prefix + str(IDENTITY_COUNTER[0])

    def embedded_html(self, debugger=False, await=[], template=HTML_EMBEDDING_TEMPLATE, div_id=None):
        """
        Translate buffered commands to static HTML.
        """
        assert type(await) is list
        await_string = json.dumps(await)
        IDENTITY_COUNTER[0] += 1
        div_id = self.unique_id()
        ##pr("id", div_id)
        debugger_string = "// Initialize static widget display with no debugging."
        if debugger:
            debugger_string = "// Debug mode for static widget display\ndebugger;"
        commands = self.buffered_commands
        js_commands = [to_javascript(c) for c in commands]
        command_string = indent_string(";\n".join(js_commands), 2)
        #return HTML_EMBEDDING_TEMPLATE % (div_id, debugger_string, div_id, command_string)
        return template.format(
            div_id=div_id,
            debugger_string=debugger_string,
            actions=command_string,
            names=await_string)

    def embed(self, debugger=False, await=[]):
        """
        Embed the buffered commands into the current notebook as static HTML.
        """
        embedded_html = self.embedded_html(debugger, await=await)
        display(HTML(embedded_html))

    def embedded_javascript(self, debugger=False, await=[], div_id=None):
        return self.embedded_html(debugger, await, template=JAVASCRIPT_EMBEDDING_TEMPLATE, div_id=div_id)

    def save_javascript(self, filename, debugger=False, await=[], div_id=None):
        out = open(filename, "w")
        js = self.embedded_javascript(debugger, await, div_id=div_id)
        out.write(js)

    def __call__(self, command):
        "Add a command to the buffered commands. Convenience."
        self.buffered_commands.append(command)
        if self.auto_flush:
            self.flush()
        return command

    def seg_flush(self, results_callback=None, level=1, segmented=BIG_SEGMENT):
        "flush a potentially large command sequence, segmented."
        return self.flush(results_callback, level, segmented)

    def flush(self, results_callback=None, level=1, segmented=None):
        "send the buffered commands and clear the buffer. Convenience."
        commands = self.buffered_commands
        self.buffered_commands = []
        return self.send_commands(commands, results_callback, level, segmented=segmented)

    def save(self, name, reference):
        """
        Proxy to save referent in the element namespace by name.
        The command to save the element is buffered and the return
        value is a reference to the element by name.
        This must be followed by a flush() to execute the command.
        """
        elt = self.element()
        save_command = elt._set(name, reference)
        # buffer the save operation
        self(save_command)
        # return the reference by name
        return getattr(elt, name)

    _jqueryUI_checked = False

    def check_jquery(self, 
        code_fn="js/jquery-ui-1.12.1/jquery-ui.js", 
        style_fn="js/jquery-ui-1.12.1/jquery-ui.css",
        timeout_milliseconds=2000, onsuccess=None, verbose=False):
        """
        Make JQuery and JQueryUI globally available for other modules.
        """
        # check whether any widget in this context has loaded jqueryUI
        # xxx shortcut will not work in lab after reload
        #if JSProxyWidget._jqueryUI_checked:
        #    if onsuccess:
        #        onsuccess()
        #    return  # Don't need to check twice
        def load_failed():
            raise ImportError("Failed to load JQueryUI in javascript context.")
        def load_succeeded():
            # mark successful load for interpreter context.
            JSProxyWidget._jqueryUI_checked = True
            if onsuccess:
                onsuccess()
        def load_jqueryUI():
            # Attach the global jquery and lodash
            #pr("loading jquery")
            self.load_css(style_fn)
            self.load_js_files([code_fn])
            #pr("sleeping to allow sync")
            #pr("rechecking load")
            self.js_init("""
                console.log("rechecking jquery load");
                if (element.dialog) {
                    console.log("recheck ok");
                    load_succeeded();
                } else {
                    console.log("recheck failed!!");
                    load_failed();
                }
            """, load_failed=load_failed, load_succeeded=load_succeeded)
            #pr ("finished with load_jqueryUI")
        if verbose:
            print("sending load logic via js_init")
        self.js_init("""
            console.log("checking jquery load")
            if (element.dialog)
            {
                console.log("jquery has been loaded")
                load_succeeded();
            } else {
                console.log("assigning jquery");
                window["jQuery"] = element.jQuery;
                window["$"] = element.jQuery;
                window["_"] = element._;
                load_jqueryUI();
            }
        """, load_succeeded=load_succeeded, load_jqueryUI=load_jqueryUI)
        if timeout_milliseconds is not None:
            def jquery_load_finished():
                return JSProxyWidget._jqueryUI_checked
            if verbose:
                print("awaiting jquery load flag")
            self.await_condition(jquery_load_finished, timeout_milliseconds)
        if verbose:
            print("finished loading jQuery etc.")

    _require_checked = False

    def _check_require_is_loaded(self, filepath="js/require.js", onsuccess=None, verbose=False,
        timeout_milliseconds=2000):
        """
        Force load require.js if window.require is not yet available.
        """
        # xxx shortcut will not work in lab after reload
        #if JSProxyWidget._require_checked:
        #    if onsuccess:
        #        onsuccess()
        #    # Don't need to check twice.
        #    return
        def load_failed():
            raise ImportError("Failed to load require.js in javascript context.")
        def load_succeeded():
            JSProxyWidget._require_checked = True
            if onsuccess:
                onsuccess()
        def load_require_js():
            self.load_js_files([filepath])
            self.js_init("""
                console.log("proxy widget: assigning require aliases.");
                element.alias_require();
                if (element.requirejs) {
                    load_succeeded();
                } else {
                    load_failed();
                }
            """, load_failed=load_failed, load_succeeded=load_succeeded)
        self.js_init("""
            console.log("checking for requirejs");
            if (element.requirejs) {
                load_succeeded();
            } else {
                load_require_js();
            }
        """, load_require_js=load_require_js, load_succeeded=load_succeeded)
        if timeout_milliseconds is not None:
            def requirejs_load_finished():
                return JSProxyWidget._require_checked 
            if verbose:
                print("awaiting requirejs load flag")
            self.await_condition(requirejs_load_finished, timeout_milliseconds)
        if verbose:
            print("finished loading jQuery etc.")

    def load_css(self, filepath, local=True):
        """
        Load a CSS text content from a file accessible by Python.
        """
        text = js_context.get_text_from_file_name(filepath, local)
        #return js_context.display_css(self, text)
        return self.append_css(text)

    def append_css(self, text):
        #https://stackoverflow.com/questions/1212500/create-a-css-rule-class-with-jquery-at-runtime?utm_medium=organic&utm_source=google_rich_qa&utm_campaign=google_rich_qa
        self.js_init(r"""
        debugger;
        element.jQuery("<style>")
            .prop("type", "text/css")
            .html("\n"+text)
            .appendTo("head");
        """, text=text)

    def require_js(self, name, filepath, local=True):
        """
        Load a javascript require.js compatible module from a file accessible by Python.
        """
        text = js_context.get_text_from_file_name(filepath, local)
        return self.load_js_module_text(name, text)

    def load_js_module_text(self, name, text):
        """
        Load a require.js module text.
        Later the module content will be available as self.element()[name]
        from Python or element[name] in js_init.
        """
        elt = self.element()
        load_call = elt._load_js_module(name, text)
        self(load_call)
        # return reference to the loaded module
        return getattr(elt, name)

    def save_new(self, name, constructor, arguments):
        """
        Construct a 'new constructor(arguments)' and save the object in the element namespace.
        Store the construction in the command buffer and return a reference to the
        new object.
        """
        new_reference = self.element().New(constructor, arguments)
        return self.save(name, new_reference)

    def save_function(self, name, arguments, body):
        """
        Buffer a command to create a JS function using "new Function(...)"
        """
        klass = self.window().Function
        return self.save_new(name, klass, list(arguments) + [body])

    def function(self, arguments, body):
        klass = self.window().Function
        return self.element().New(klass, list(arguments) + [body])

    handle_results_exception = None

    def handle_results(self, new):
        "Callback for when results arrive after the JS View executes commands."
        if self.verbose:
            print ("got results", new)
        [identifier, json_value] = new
        i2c = self.identifier_to_callback
        results_callback = i2c.get(identifier)
        if results_callback is not None:
            del i2c[identifier]
            try:
                results_callback(json_value)
            except Exception as e:
                #pr ("handle results exception " + repr(e))
                self.handle_results_exception = e
                self.error_msg = repr(e)
                raise

    handle_callback_results_exception = None
    last_callback_results = None

    def handle_callback_results(self, new):
        "Callback for when the JS View sends an event notification."
        self.last_callback_results = new
        if self.verbose:
            print ("got callback results", new)
        [identifier, json_value, arguments, counter] = new
        i2c = self.identifier_to_callback
        results_callback = i2c.get(identifier)
        self.status = "call back to " + repr(results_callback)
        if results_callback is not None:
            try:
                results_callback(json_value, arguments)
            except Exception as e:
                #pr ("handle results callback exception " +repr(e))
                self.handle_callback_results_exception = e
                self.error_msg = repr(e)
                raise

    def send_command(self, command, results_callback=None, level=1):
        "Send a single command to the JS View."
        return self.send_commands([command], results_callback, level)

    def send_commands(self, commands_iter, results_callback=None, level=1, segmented=None):
        """Send several commands fo the JS View.
        If segmented is a positive integer then the commands payload will be pre-encoded
        as a json string and sent in segments of that length
        """
        count = self.counter
        self.counter = count + 1
        qcommands = list(map(quoteIfNeeded, commands_iter))
        commands = validate_commands(qcommands)
        if self.rendered:
            # also send any commands awaiting the render event.
            if self.commands_awaiting_render:
                commands = commands + self.commands_awaiting_render
                self.commands_awaiting_render = None
            payload = [count, commands, level]
            if results_callback is not None:
                self.identifier_to_callback[count] = results_callback
            # send the command using the commands traitlet which is mirrored to javascript.
            #self.commands = payload
            if segmented and segmented > 0:
                self.send_segmented_message(COMMANDS_FRAGMENT, COMMANDS_FINAL, payload, segmented)
            else:
                self.send_custom_message(COMMANDS, payload)
            self.last_commands_sent = payload
            return payload
        else:
            # wait for render event before sending commands.
            ##pr "waiting for render!", commands
            self.commands_awaiting_render.extend(commands)
            return ("awaiting render", commands)

    def send_segmented_message(self, frag_ind, final_ind, payload, segmented):
        json_str = json.dumps(payload)
        len_json = len(json_str)
        cursor = 0
        # don't reallocate large string tails...
        while len_json - cursor > segmented:
            next_cursor = cursor + segmented
            json_fragment = json_str[cursor: next_cursor]
            # send the fragment
            self.send_custom_message(frag_ind, json_fragment)
            cursor = next_cursor
        json_tail = json_str[cursor:]
        self.send_custom_message(final_ind, json_tail)
    
    def evaluate(self, command, level=1, timeout=3000):
        "Send one command and wait for result.  Return result."
        results = self.evaluate_commands([command], level, timeout)
        assert len(results) == 1
        return results[0]

    def evaluate_commands(self, commands_iter, level=1, timeout=3000):
        "Send commands and wait for results.  Return results."
        # inspired by https://github.com/jdfreder/ipython-jsobject/blob/master/jsobject/utils.py
        result_list = []

        def evaluation_callback(json_value):
            result_list.append(json_value)

        self.send_commands(commands_iter, evaluation_callback, level)

        def evaluate_finished():
            return len(result_list) > 0

        self.await_condition(evaluate_finished, timeout)

        return result_list[0]

    def await_condition(self, condition, timeout_milliseconds, delay=0.01, verbose=False):
        """
        Wait for some condition to be caused by Javascript, or timeout
        """
        start = time.time()
        count = 0
        if verbose:
            print("entering await loop for ", condition)
        while not condition():
            count += 1
            if verbose and count % 500 == 0:
                print(count, " awaiting ", condition)
            if time.time() - start > timeout_milliseconds / 1000.0:
                self.error_msg = "Time out in await_condition"
                raise Exception("Timeout condition: " + repr((timeout, condition)))
            ip.kernel.do_one_iteration()
            time.sleep(delay)
        if verbose:
            print("done awaiting ", condition)

    def seg_callback(self, callback_function, data, level=1, delay=False, segmented=BIG_SEGMENT):
        """
        Proxy callback with message segmentation to support potentially large
        messages.
        """
        return self.callback(callback_function, data, level, delay, segmented)

    def callable(self, function_or_method, level=1, delay=False, segmented=None):
        """
        Simplified callback protocol.
        Map function_or_method to a javascript function js_function
        Calls to js_function(x, y, z)
        will trigger calls to function_or_method(x, y, z)
        where x, y, z are json compatible values.
        """
        data = repr(function_or_method)
        def callback_function(_data, arguments):
            count = 0
            # construct the Python argument list from argument mapping
            py_arguments = []
            while 1:
                argstring = str(count)
                if argstring in arguments:
                    argvalue = arguments[argstring]
                    py_arguments.append(argvalue)
                    count += 1
                else:
                    break
            function_or_method(*py_arguments)
        return self.callback(callback_function, data, level, delay, segmented)

    def callback(self, callback_function, data, level=1, delay=False, segmented=None):
        "Create a 'proxy callback' to receive events detected by the JS View."
        assert level > 0, "level must be positive " + repr(level)
        assert level <= 5, "level cannot exceed 5 " + repr(level)
        assert segmented is None or (type(segmented) is int and segmented > 0), "bad segment " + repr(segmented)
        count = self.counter
        self.counter = count + 1
        command = CallMaker("callback", count, data, level, segmented)
        #if delay:
        #    callback_function = delay_in_thread(callback_function)
        self.identifier_to_callback[count] = callback_function
        return command

    def forget_callback(self, callback_function):
        "Remove all uses of callback_function in proxy callbacks (Python side only)."
        i2c = self.identifier_to_callback
        deletes = [i for i in i2c if i2c[i] == callback_function]
        for i in deletes:
            del i2c[i]

    def js_debug(self, *arguments):
        """
        Break in the Chrome debugger (only if developer tools is open)
        """
        if not arguments:
            arguments = [self.element()]
        return self.send_command(self.function(["element"], "debugger;")(self.element()))

    def print_status(self):
        status_slots = """
            results
            auto_flush _last_message_data _json_accumulator _last_custom_message_error
            _last_accumulated_json _jqueryUI_checked _require_checked
            handle_results_exception last_callback_results
            """
        print (repr(self) + " STATUS:")
        for slot_name in status_slots.split():
            print ("\t::::: " + slot_name + " :::::")
            print (getattr(self, slot_name, "MISSING"))

    def element(self):
        "Return a proxy reference to the Widget JQuery element this.$el."
        return CommandMaker("element")

    def window(self):
        "Return a proxy reference to the browser window top level name space."
        return CommandMaker("window")

    def load_js_files(self, filenames, verbose=False, delay=0.1, force=False, local=True):
        #import js_context
        js_context.load_if_not_loaded(self, filenames, verbose=verbose, delay=delay, force=force, local=local)


def validate_commands(commands, top=True):
    """
    Validate a command sequence (and convert to list format if needed.)
    """
    return [validate_command(c, top) for c in commands]


def validate_command(command, top=True):
    # convert CommandMaker to list format.
    if isinstance(command, CommandMaker):
        command = command._cmd()
    ty = type(command)
    if ty is list:
        indicator = command[0]
        remainder = command[1:]
        if indicator == "element" or indicator == "window":
            assert len(remainder) == 0
        elif indicator == "method":
            target = remainder[0]
            name = remainder[1]
            args = remainder[2:]
            target = validate_command(target, top=True)
            assert type(name) is str, "method name must be a string " + repr(name)
            args = validate_commands(args, top=False)
            remainder = [target, name] + args
        elif indicator == "function":
            target = remainder[0]
            args = remainder[1:]
            target = validate_command(target, top=True)
            args = validate_commands(args, top=False)
            remainder = [target] + args
        elif indicator == "id" or indicator == "bytes":
            assert len(remainder) == 1, "id or bytes takes one argument only " + repr(remainder)
        elif indicator == "list":
            remainder = validate_commands(remainder, top=False)
        elif indicator == "dict":
            [d] = remainder
            d = dict((k, validate_command(d[k], top=False)) for k in d)
            remainder = [d]
        elif indicator == "callback":
            [numerical_identifier, untranslated_data, level, segmented] = remainder
            assert type(numerical_identifier) is int, \
                "must be integer " + repr(numerical_identifier)
            assert type(level) is int, \
                "must be integer " + repr(level)
            assert (segmented is None) or (type(segmented) is int and segmented > 0), \
                "must be None or positive integer " + repr(segmented)
        elif indicator == "get":
            [target, name] = remainder
            target = validate_command(target, top=True)
            name = validate_command(name, top=False)
            remainder = [target, name]
        elif indicator == "set":
            [target, name, value] = remainder
            target = validate_command(target, top=True)
            name = validate_command(name, top=False)
            value = validate_command(value, top=False)
            remainder = [target, name, value]
        elif indicator == "null":
            [target] = remainder
            remainder = [validate_command(target, top=False)]
        else:
            raise ValueError("bad indicator " + repr(indicator))
        command = [indicator] + remainder
    elif top:
        raise ValueError("top level command must be a list " + repr(command))
    # Non-lists are untranslated (but should be JSON compatible).
    return command

def indent_string(s, level, indent="    "):
    lindent = indent * level
    return s.replace("\n", "\n" + lindent)

def to_javascript(thing, level=0, indent=None, comma=","):
    if isinstance(thing, CommandMaker):
        return thing.javascript(level)
    else:
        ty = type(thing)
        json_value = None
        if ty is dict:
            L = {"%s: %s" % (to_javascript(key), to_javascript(thing[key]))
                for key in thing.keys()}
            json_value = "{%s}" % (comma.join(L))
        elif ty is list or ty is tuple:
            L = [to_javascript(x) for x in thing]
            json_value = "[%s]" % (comma.join(L))
        elif ty is bytearray:
            inner = list(map(int, thing))
            # Note: no line breaks for binary data.
            json_value = "Uint8Array(%s)" % inner
        elif json_value is None:
            json_value = json.dumps(thing, indent=indent)
        return indent_string(json_value, level)


class CommandMaker(object):

    """
    Superclass for command proxy objects.
    Directly implements top level objects like "window" and "element".
    """

    top_level_names = "window element".split()

    def __init__(self, name="window"):
        assert name in self.top_level_names
        self.name = name

    def __repr__(self):
        return self.javascript()

    def javascript(self, level=0):
        "Return javascript text intended for this command"
        return indent_string(self.name, level)
    
    def _cmd(self):
        "Translate self to JSON representation for transmission to view."
        return [self.name]

    def __getattr__(self, name):
        "Proxy to get a property of a jS object."
        return MethodMaker(self, name)

    # for parallelism to _set
    _get = __getattr__

    # in javascript these are essentially the same thing.
    __getitem__ = __getattr__

    def _set(self, name, value):
        "Proxy to set a property of a JS object."
        return SetMaker(self, name, value)

    def __call__(self, *args):
        "Proxy to call a JS object."
        raise ValueError("top level object cannot be called.")

    def _null(self):
        "Proxy to discard results of JS evaluation."
        return ["null", self]


# For attribute access use target[value] instead of target.name
# because sometimes the value will not be a string.


Set_Template = """
f = function () {
    var target = %s;
    var attribute = %s;
    var value = %s;
    target[attribute] = value;
    return target;
};
f();
""".strip()


class SetMaker(CommandMaker):
    """
    Proxy container to set target.name = value.
    For chaining the result is a reference to the target.
    """

    def __init__(self, target, name, value):
        self.target = target
        self.name = name
        self.value = value

    def javascript(self, level=0):
        innerlevel = 2
        target = to_javascript(self.target, innerlevel)
        value = to_javascript(self.value, innerlevel)
        name = to_javascript(self.name, innerlevel)
        T = Set_Template % (target, name, value)
        return indent_string(T, level)

    def _cmd(self):
        #target = validate_command(self.target, False)
        #@value = validate_command(self.value, False)
        target = self.target
        value = self.value
        return ["set", target, self.name, value]


class MethodMaker(CommandMaker):
    """
    Proxy reference to a property or method of a JS object.
    """

    def __init__(self, target, name):
        self.target = target
        self.name = name

    def javascript(self, level=0):
        # use target[value] notation (see comment above)
        target = to_javascript(self.target)
        attribute = to_javascript(self.name)
        # add a line break in case of long chains
        T = "%s\n[%s]" % (target, attribute)
        return indent_string(T, level)

    def _cmd(self):
        #target = validate_command(self.target, False)
        target = self.target
        return ["get", target, self.name]

    def __call__(self, *args):
        return CallMaker("method", self.target, self.name, *args)



def format_args(args):
    args_js = [to_javascript(a, 1) for a in args]
    args_inner = ",\n".join(args_js)
    return "(%s)" % args_inner


class CallMaker(CommandMaker):
    """
    Proxy reference to a JS method call or function call.
    If kind == "method" and args == [target, name, arg0, ..., argn]
    Then proxy value is target.name(arg0, ..., argn)
    """

    def __init__(self, kind, *args):
        self.kind = kind
        self.args = quoteLists(args)

    def javascript(self, level=0):
        kind = self.kind
        args = self.args
        # Add newlines in case of long chains.
        if kind == "function":
            function_desc = args[0]
            function_args = args[1:]
            function_value = to_javascript(function_desc)
            call_js = "%s\n%s" % (function_value, format_args(function_args))
            return indent_string(call_js, level)
        elif kind == "method":
            target_desc = args[0]
            name = args[1]
            method_args = args[2:]
            target_value = to_javascript(target_desc)
            name_value = to_javascript(name)
            method_js = "%s\n[%s]\n%s" % (target_value, name_value, format_args(method_args))
            return indent_string(method_js, level)
        else:
            # This should never be executed, but the javascript
            # translation is useful for debugging.
            message = "Warning: External callable " + repr(self.args)
            return "function() {alert(%s);}" % to_javascript(message)

    def __call__(self, *args):
        """
        Call the callable returned by the function or method call.
        """
        return CallMaker("function", self, *args)

    def _cmd(self):
        return [self.kind] + self.args #+ validate_commands(self.args, False)


class LiteralMaker(CommandMaker):
    """
    Proxy to make a literal dictionary or list which may contain other
    proxy references.
    """

    indicators = {dict: "dict", list: "list", bytearray: "bytes"}

    def __init__(self, thing):
        self.thing = thing

    def javascript(self, level=0):
        thing_fmt = to_javascript(self.thing)
        return indent_string(thing_fmt, level)

    def _cmd(self):
        thing = self.thing
        ty = type(thing)
        indicator = self.indicators.get(type(thing))
        #return [indicator, thing]
        if indicator:
            if ty is list:
                return [indicator] + quoteLists(thing)
            elif ty is dict:
                return [indicator, dict((k, quoteIfNeeded(thing[k])) for k in thing)]
            elif ty is bytearray:
                return [indicator, bytearray_to_hex(thing)]
            else:
                raise ValueError("can't translate " + repr(ty))
        return thing


def quoteIfNeeded(arg):
    if type(arg) in LiteralMaker.indicators:
        return LiteralMaker(arg)
    return arg

def quoteLists(args):
    "Wrap lists or dictionaries in the args in LiteralMakers"
    return [quoteIfNeeded(x) for x in args]

