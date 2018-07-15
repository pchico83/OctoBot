import threading
import time
import logging
import copy

from backtesting.backtesting import Backtesting
from config.cst import TimeFramesMinutes, MINUTE_TO_SECONDS, PriceIndexes
from tools.time_frame_manager import TimeFrameManager


class SymbolTimeFramesDataUpdaterThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.evaluator_threads_manager_by_time_frame = {}
        self.refreshed_times = {}
        self.time_frame_last_update = {}
        self.keep_running = True
        self.logger = logging.getLogger(self.__class__.__name__)

    # add a time frame to watch and its related evaluator thread manager
    def register_evaluator_thread_manager(self, time_frame, evaluator_thread_manager):
        self.evaluator_threads_manager_by_time_frame[time_frame] = evaluator_thread_manager

    def stop(self):
        self.keep_running = False

    def get_refreshed_times(self, time_frame):
        return self.refreshed_times[time_frame]

    # notify the time frame's evaluator thread manager to refresh its data
    def _refresh_data(self, time_frame, limit=None):
        evaluator_thread_manager_to_notify = self.evaluator_threads_manager_by_time_frame[time_frame]
        
        numpy_candle_data = copy.deepcopy(evaluator_thread_manager_to_notify.exchange.get_symbol_prices(
                evaluator_thread_manager_to_notify.symbol,
                evaluator_thread_manager_to_notify.time_frame,
                limit=limit,
                return_list=False))
        
        evaluator_thread_manager_to_notify.evaluator.set_data(numpy_candle_data)
        self.refreshed_times[time_frame] += 1
        evaluator_thread_manager_to_notify.notify(self.__class__.__name__)

    # start background refresher
    def run(self):
        time_frames = self.evaluator_threads_manager_by_time_frame.keys()
        # sort time frames to update them in order of accuracy
        time_frames = TimeFrameManager.sort_time_frames(time_frames)

        if time_frames:
            max_sleeping_time = 2

            # figure out from an evaluator if back testing is running for this symbol
            evaluator_thread_manager = next(iter(self.evaluator_threads_manager_by_time_frame.values()))
            back_testing_enabled = Backtesting.enabled(evaluator_thread_manager.get_evaluator().get_config())
            if back_testing_enabled:
                exchange = evaluator_thread_manager.exchange.get_exchange()
                exchange.init_candles_offset(time_frames, evaluator_thread_manager.symbol)

            # init refreshed_times at 0 for each time frame
            self.refreshed_times = {key: 0 for key in time_frames}
            # init last refresh times at 0 for each time frame
            self.time_frame_last_update = {key: 0 for key in time_frames}

            while self.keep_running:
                now = time.time()

                for time_frame in time_frames:
                    if back_testing_enabled:
                        if exchange.should_update_data(time_frame):
                             self._refresh_data(time_frame)

                    # if data from this time frame needs an update
                    elif now - self.time_frame_last_update[time_frame] >= \
                            TimeFramesMinutes[time_frame] * MINUTE_TO_SECONDS:
                        try:
                            self._refresh_data(time_frame)
                        except Exception as e:
                            self.logger.error("error when refreshing data for time frame {0} for {1}: {2}"
                                              .format(time_frame, evaluator_thread_manager.symbol, e))
                            self.logger.exception(e)
                        self.time_frame_last_update[time_frame] = time.time()

                if not back_testing_enabled:
                    sleeping_time = max_sleeping_time - (time.time() - now)
                    if sleeping_time > 0:
                        time.sleep(sleeping_time)
                else:
                    while not self.ensure_finished_other_threads_tasks(time_frames[0]):
                        time.sleep(0.001)
        else:
            self.logger.warning("no time frames to monitor, going to sleep.")

    def ensure_finished_other_threads_tasks(self, smallest_timeframe):
        data = self.evaluator_threads_manager_by_time_frame[smallest_timeframe].evaluator.get_data()
        simulated_time = data[PriceIndexes.IND_PRICE_TIME.value][-1]
        for evaluator_thread_manager in self.evaluator_threads_manager_by_time_frame.values():
            symbol_evaluator = evaluator_thread_manager.symbol_evaluator
            if symbol_evaluator.get_deciders_are_busy():
                return False
            else:
                for trader_simulators in symbol_evaluator.trader_simulators.values():
                    trader_simulators.order_manager.force_update_order_status(simulated_time=simulated_time)
        return True

